from __future__ import annotations

import argparse
import getpass
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
from typing import Sequence

from .client import HttpCenterClient, capability_payload
from .daemon import TrainingWorkerDaemon
from .identity import load_or_create_identity, load_worker_token, store_worker_token
from .ledger import WorkerLedger
from .resources import ResourceCollector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datapilot-training-worker",
        description="Run the read-only DataPilot Training Worker v1 inventory daemon.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_default_state_dir(),
        help="Directory for the private worker identity and local task ledger.",
    )
    parser.add_argument(
        "--disk-path",
        action="append",
        type=Path,
        dest="disk_paths",
        help="Filesystem root to inventory. May be repeated; defaults to '/'.",
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument(
        "--center-base-url",
        help="Fixed HTTP(S) control-plane base URL; required for online mode.",
    )
    parser.add_argument(
        "--node-ref",
        help="Enrolled control-plane node reference; persisted usage requires this value.",
    )
    parser.add_argument(
        "--enrollment-token-stdin",
        action="store_true",
        help="Read the one-time enrollment token from a hidden TTY prompt or stdin; never from argv.",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect one payload as JSON and exit without contacting a center.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    identity = load_or_create_identity(args.state_dir)
    ledger = WorkerLedger(args.state_dir / "worker-ledger.sqlite")
    collector = ResourceCollector(disk_paths=args.disk_paths or [Path("/")])
    center_client = None
    worker_token = load_worker_token(args.state_dir)
    node_ref = args.node_ref
    enrollment_token = (
        _read_enrollment_token() if args.enrollment_token_stdin else None
    )
    if args.center_base_url:
        center_client = HttpCenterClient(
            center_base_url=args.center_base_url,
            worker_token=worker_token,
            node_ref=node_ref,
            timeout_seconds=args.request_timeout_seconds,
        )
        if enrollment_token:
            if worker_token is not None:
                parser_error("worker is already enrolled; remove the existing token before re-enrollment")
            initial_resources = collector.collect()
            enrollment = center_client.enroll(
                identity,
                enrollment_token,
                capability_payload(initial_resources),
            )
            store_worker_token(args.state_dir, enrollment.worker_token)
            worker_token = enrollment.worker_token
            node_ref = enrollment.node_ref
        if worker_token is None or node_ref is None:
            parser_error("online mode requires --node-ref and an enrolled worker token")
    elif enrollment_token or args.node_ref:
        parser_error("--center-base-url is required with enrollment or node settings")
    daemon = TrainingWorkerDaemon(
        identity=identity,
        ledger=ledger,
        resource_collector=collector,
        center_client=center_client,
        interval_seconds=args.interval_seconds,
    )
    if args.once:
        print(json.dumps(daemon.run_once(), ensure_ascii=False, sort_keys=True))
        return 0

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, lambda _signum, _frame: daemon.stop())
    daemon.run_forever()
    return 0


def _default_state_dir() -> Path:
    configured = os.environ.get("DATAPILOT_WORKER_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "datapilot-training-worker"


def parser_error(message: str) -> None:
    raise SystemExit(message)


def _read_enrollment_token(stream: io.TextIOBase | None = None) -> str:
    if stream is None and sys.stdin.isatty():
        token = getpass.getpass("Enrollment token: ")
    else:
        source = stream or sys.stdin
        token = source.readline(258).rstrip("\r\n")
    if not re.fullmatch(r"enroll_[A-Za-z0-9_-]{33,249}", token):
        parser_error("stdin did not contain a valid enrollment token")
    return token
