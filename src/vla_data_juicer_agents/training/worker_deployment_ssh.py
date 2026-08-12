"""Pinned-host-key OpenSSH adapter for the system Worker deployer.

All remote behaviour is implemented by one immutable Python installer program
and a closed operation enum.  No API value can select an executable or submit
shell text.  SSH and sudo credentials remain context-local and are carried only
through askpass/FIFO or subprocess stdin respectively.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
import base64
import json
import re
from typing import Protocol, cast

from .ssh_bootstrap import (
    OpenSshBackend,
    OpenSshPasswordBackend,
    PinnedHostKey,
    RemoteExecution,
    SshEndpoint,
    SshSession,
)
from .worker_deployment import (
    DirectorySpec,
    DeploymentPrivilege,
    FixedCommandResult,
    ManagedFileSpec,
    PASSWORDLESS_SUDO_PROBE_ARGV,
    PASSWORD_SUDO_PROBE_ARGV,
    ROOT_IDENTITY_PROBE_ARGV,
    ServiceAccountSpec,
    SudoPasswordMode,
    TrainingNodeDeploymentError,
    WorkerRelease,
    inspect_deployment_privilege,
)


class _FixedArgvSshSession(SshSession, Protocol):
    def run_fixed_argv(
        self,
        remote_argv: tuple[str, ...],
        *,
        stdin_payload: bytes,
        timeout_seconds: int,
        operation_name: str,
    ) -> RemoteExecution: ...


class _Operation(StrEnum):
    ENSURE_ACCOUNT = "ensure_account"
    ENSURE_DIRECTORY = "ensure_directory"
    INSTALL_RELEASE = "install_release"
    WRITE_FILE = "write_file"
    ACTIVATE_RELEASE = "activate_release"
    IS_ENROLLED = "is_enrolled"
    ENROLL = "enroll"
    START_SERVICE = "start_service"
    IS_ACTIVE = "is_active"


_REMOTE_INSTALLER = r'''import grp
import hashlib
import json
import os
import pathlib
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

operation = sys.argv[1]
arguments = json.loads(sys.argv[2])

def output(changed=False, value=None):
    print(json.dumps({"changed": bool(changed), "value": value}, separators=(",", ":")))

def owner_ids(owner, group):
    return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid

def ensure_metadata(path, owner, group, mode):
    uid, gid = owner_ids(owner, group)
    current = path.stat()
    changed = False
    if current.st_uid != uid or current.st_gid != gid:
        os.chown(path, uid, gid, follow_symlinks=False)
        changed = True
    if stat.S_IMODE(current.st_mode) != mode:
        os.chmod(path, mode, follow_symlinks=False)
        changed = True
    return changed

def atomic_file(path, content, owner, group, mode):
    if path.is_symlink():
        raise RuntimeError("managed path is a symbolic link")
    if path.exists() and path.read_bytes() == content:
        return ensure_metadata(path, owner, group, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".datapilot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        uid, gid = owner_ids(owner, group)
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True

if operation == "ensure_account":
    username = arguments["username"]
    group = arguments["group"]
    changed = False
    try:
        group_record = grp.getgrnam(group)
    except KeyError:
        subprocess.run(["/usr/sbin/groupadd", "--system", group], check=True)
        group_record = grp.getgrnam(group)
        changed = True
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        subprocess.run([
            "/usr/sbin/useradd", "--system", "--gid", group,
            "--home-dir", arguments["home_directory"], "--no-create-home",
            "--shell", arguments["login_shell"], username,
        ], check=True)
        account = pwd.getpwnam(username)
        changed = True
    if account.pw_gid != group_record.gr_gid or account.pw_dir != arguments["home_directory"]:
        raise RuntimeError("existing worker account has incompatible ownership or home")
    if account.pw_shell != arguments["login_shell"]:
        subprocess.run([
            "/usr/sbin/usermod", "--shell", arguments["login_shell"], username,
        ], check=True)
        changed = True
    output(changed)
elif operation == "ensure_directory":
    path = pathlib.Path(arguments["path"])
    if path.is_symlink():
        raise RuntimeError("managed directory is a symbolic link")
    changed = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    changed = ensure_metadata(
        path, arguments["owner"], arguments["group"], arguments["mode"]
    ) or changed
    output(changed)
elif operation == "install_release":
    content = sys.stdin.buffer.read()
    if hashlib.sha256(content).hexdigest() != arguments["sha256"]:
        raise RuntimeError("release digest mismatch on target")
    release_directory = pathlib.Path(arguments["release_directory"])
    release_directory.mkdir(parents=True, exist_ok=True)
    ensure_metadata(release_directory, "root", "root", 0o755)
    changed = atomic_file(
        pathlib.Path(arguments["artifact_path"]), content,
        arguments["owner"], arguments["group"], arguments["mode"]
    )
    output(changed)
elif operation == "write_file":
    changed = atomic_file(
        pathlib.Path(arguments["path"]), sys.stdin.buffer.read(),
        arguments["owner"], arguments["group"], arguments["mode"]
    )
    output(changed)
elif operation == "activate_release":
    target = arguments["release_directory"]
    link = pathlib.Path(arguments["current_link"])
    if not pathlib.Path(target).is_dir():
        raise RuntimeError("release directory does not exist")
    if link.is_symlink() and os.readlink(link) == target:
        output(False)
    else:
        temporary = pathlib.Path(str(link) + ".new")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
        output(True)
elif operation == "is_enrolled":
    token = pathlib.Path(arguments["state_directory"]) / "worker-token"
    output(False, token.is_file() and not token.is_symlink())
elif operation == "enroll":
    token = sys.stdin.buffer.read(1025)
    if not token or len(token) > 1024 or b"\n" in token or b"\r" in token:
        raise RuntimeError("invalid enrollment input")
    runuser = shutil.which("runuser")
    if runuser is None:
        raise RuntimeError("runuser is unavailable")
    command = [
        runuser, "-u", arguments["run_as"], "--", "/usr/bin/python3",
        arguments["artifact_path"], "--state-dir", arguments["state_directory"],
        "--center-base-url", arguments["center_base_url"], "--node-ref",
        arguments["node_ref"], "--enrollment-token-stdin", "--once",
    ]
    environment = os.environ.copy()
    center_ca_path = arguments.get("center_ca_path")
    if center_ca_path:
        environment["DATAPILOT_CENTER_CA_CERT_PATH"] = center_ca_path
    subprocess.run(
        command,
        input=token + b"\n",
        check=True,
        env=environment,
    )
    output(True)
elif operation == "start_service":
    unit = arguments["unit_name"]
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
    enabled = subprocess.run(
        ["/usr/bin/systemctl", "is-enabled", "--quiet", unit]
    ).returncode == 0
    active = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", unit]
    ).returncode == 0
    subprocess.run(["/usr/bin/systemctl", "enable", "--now", unit], check=True)
    output(not enabled or not active)
elif operation == "is_active":
    active = subprocess.run([
        "/usr/bin/systemctl", "is-active", "--quiet", arguments["unit_name"]
    ]).returncode == 0
    output(False, active)
else:
    raise RuntimeError("unsupported deployment operation")
'''


_REMOTE_SUDO_BRIDGE = r'''import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile

password = bytearray(sys.stdin.buffer.readline(1025))
if not password or len(password) > 1024 or not password.endswith(b"\n"):
    raise RuntimeError("invalid sudo input")
payload = sys.stdin.buffer.read()
temporary = pathlib.Path(tempfile.mkdtemp(prefix="datapilot-sudo-", dir="/tmp"))
os.chmod(temporary, stat.S_IRWXU)
pipe = temporary / "password.pipe"
helper = temporary / "askpass"
pipe_descriptor = None
try:
    os.mkfifo(pipe, stat.S_IRUSR | stat.S_IWUSR)
    pipe_descriptor = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
    os.write(pipe_descriptor, password)
    helper.write_bytes(
        b"#!/usr/bin/python3\nimport os,sys\n"
        b"with open(os.environ['DATAPILOT_SUDO_ASKPASS_PIPE'], 'rb', buffering=0) as stream:\n"
        b" sys.stdout.buffer.write(stream.read(2048))\n"
    )
    os.chmod(helper, stat.S_IRWXU)
    environment = os.environ.copy()
    environment.update({
        "SUDO_ASKPASS": str(helper),
        "DATAPILOT_SUDO_ASKPASS_PIPE": str(pipe),
    })
    executed = subprocess.run(
        [
            "/usr/bin/sudo", "-A", "-k", "-p",
            "DataPilot sudo password:", "--", *sys.argv[1:],
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    sys.stdout.buffer.write(executed.stdout)
    sys.stderr.buffer.write(executed.stderr[-65536:])
    raise SystemExit(executed.returncode)
finally:
    if pipe_descriptor is not None:
        os.close(pipe_descriptor)
    for index in range(len(password)):
        password[index] = 0
    shutil.rmtree(temporary, ignore_errors=True)
'''


_REMOTE_SOURCE_LOADER = "import base64;exec(base64.b64decode(__import__('sys').argv.pop(1)))"


def _encoded_remote_source(source: str) -> str:
    return base64.b64encode(source.encode("utf-8")).decode("ascii")


@dataclass(slots=True)
class OpenSshWorkerDeploymentBackend:
    """System deployment backend scoped to an already authenticated SSH context."""

    _session: _FixedArgvSshSession = field(repr=False)
    _ssh_password: str | None = field(default=None, repr=False)
    _effective_sudo_password: str | None = field(default=None, repr=False, init=False)
    _privilege: DeploymentPrivilege | None = field(default=None, init=False)

    @classmethod
    @contextmanager
    def password_session(
        cls,
        *,
        endpoint: SshEndpoint,
        host_key: PinnedHostKey,
        password: str,
        connect_timeout_seconds: int = 10,
    ) -> Iterator[OpenSshWorkerDeploymentBackend]:
        known_hosts_line = host_key.known_hosts_line(endpoint)
        transport = OpenSshPasswordBackend()
        with transport.open_password_session(
            endpoint=endpoint,
            known_hosts_line=known_hosts_line,
            password=password,
            connect_timeout_seconds=connect_timeout_seconds,
        ) as session:
            backend = cls(cast(_FixedArgvSshSession, session), _ssh_password=password)
            try:
                yield backend
            finally:
                backend.clear_ephemeral_credentials()

    @classmethod
    @contextmanager
    def private_key_session(
        cls,
        *,
        endpoint: SshEndpoint,
        host_key: PinnedHostKey,
        private_key: bytes,
        connect_timeout_seconds: int = 10,
    ) -> Iterator[OpenSshWorkerDeploymentBackend]:
        known_hosts_line = host_key.known_hosts_line(endpoint)
        transport = OpenSshBackend()
        with transport.open_session(
            endpoint=endpoint,
            known_hosts_line=known_hosts_line,
            private_key=private_key,
            connect_timeout_seconds=connect_timeout_seconds,
        ) as session:
            yield cls(cast(_FixedArgvSshSession, session))

    def clear_ephemeral_credentials(self) -> None:
        """Drop SSH and sudo password references owned by this adapter."""

        self._ssh_password = None
        self._effective_sudo_password = None

    def inspect_privilege(
        self,
        *,
        sudo_password_mode: SudoPasswordMode,
        sudo_password: str | None,
    ) -> DeploymentPrivilege:
        self._effective_sudo_password = (
            self._ssh_password
            if sudo_password_mode is SudoPasswordMode.SAME_AS_SSH
            else sudo_password
        )
        privilege = inspect_deployment_privilege(
            self,
            sudo_password_mode=sudo_password_mode,
            ssh_password=self._ssh_password,
            sudo_password=sudo_password,
        )
        self._privilege = privilege
        return privilege

    def run_fixed_privilege_probe(
        self,
        argv: tuple[str, ...],
        *,
        stdin_secret: str | None,
        timeout_seconds: int,
    ) -> FixedCommandResult:
        if argv not in {
            ROOT_IDENTITY_PROBE_ARGV,
            PASSWORDLESS_SUDO_PROBE_ARGV,
            PASSWORD_SUDO_PROBE_ARGV,
        }:
            raise ValueError("Privilege runner rejected a non-catalogue argv")
        stdin = b"" if stdin_secret is None else stdin_secret.encode("utf-8") + b"\n"
        result = self._session.run_fixed_argv(
            argv,
            stdin_payload=stdin,
            timeout_seconds=timeout_seconds,
            operation_name="privilege_probe",
        )
        return FixedCommandResult(result.return_code, result.stdout)

    def ensure_service_account(
        self,
        spec: ServiceAccountSpec,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.ENSURE_ACCOUNT,
            {
                "username": spec.username,
                "group": spec.group,
                "home_directory": spec.home_directory,
                "login_shell": spec.login_shell,
            },
            privilege=privilege,
        )

    def ensure_directory(
        self,
        spec: DirectorySpec,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.ENSURE_DIRECTORY,
            {
                "path": spec.path,
                "owner": spec.owner,
                "group": spec.group,
                "mode": spec.mode,
            },
            privilege=privilege,
        )

    def install_release(
        self,
        release: WorkerRelease,
        *,
        owner: str,
        group: str,
        mode: int,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.INSTALL_RELEASE,
            {
                "release_directory": release.release_directory,
                "artifact_path": release.artifact_path,
                "sha256": release.sha256,
                "owner": owner,
                "group": group,
                "mode": mode,
            },
            payload=release.artifact,
            timeout_seconds=300,
            privilege=privilege,
        )

    def write_managed_file(
        self,
        spec: ManagedFileSpec,
        content: bytes,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.WRITE_FILE,
            {
                "path": spec.path,
                "owner": spec.owner,
                "group": spec.group,
                "mode": spec.mode,
            },
            payload=content,
            privilege=privilege,
        )

    def activate_release(
        self,
        *,
        release_directory: str,
        current_link: str,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.ACTIVATE_RELEASE,
            {"release_directory": release_directory, "current_link": current_link},
            privilege=privilege,
        )

    def worker_is_enrolled(self, *, privilege: DeploymentPrivilege) -> bool:
        return bool(
            self._operation_value(
                _Operation.IS_ENROLLED,
                {"state_directory": "/var/lib/datapilot-training-worker"},
                privilege=privilege,
            )
        )

    def enroll_worker(
        self,
        *,
        artifact_path: str,
        state_directory: str,
        center_base_url: str,
        node_ref: str,
        enrollment_token: str,
        center_ca_path: str | None,
        run_as: str,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.ENROLL,
            {
                "artifact_path": artifact_path,
                "state_directory": state_directory,
                "center_base_url": center_base_url,
                "node_ref": node_ref,
                "center_ca_path": center_ca_path,
                "run_as": run_as,
            },
            payload=enrollment_token.encode("utf-8"),
            timeout_seconds=120,
            privilege=privilege,
        )

    def reload_enable_and_start_system_service(
        self,
        unit_name: str,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return self._operation(
            _Operation.START_SERVICE,
            {"unit_name": unit_name},
            privilege=privilege,
        )

    def system_service_is_active(
        self,
        unit_name: str,
        *,
        privilege: DeploymentPrivilege,
    ) -> bool:
        return bool(
            self._operation_value(
                _Operation.IS_ACTIVE,
                {"unit_name": unit_name},
                privilege=privilege,
            )
        )

    def _operation(
        self,
        operation: _Operation,
        arguments: dict[str, object],
        *,
        privilege: DeploymentPrivilege,
        payload: bytes = b"",
        timeout_seconds: int = 60,
    ) -> bool:
        parsed = self._execute(
            operation,
            arguments,
            privilege=privilege,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        return parsed["changed"] is True

    def _operation_value(
        self,
        operation: _Operation,
        arguments: dict[str, object],
        *,
        privilege: DeploymentPrivilege,
    ) -> object:
        return self._execute(
            operation,
            arguments,
            privilege=privilege,
        )["value"]

    def _execute(
        self,
        operation: _Operation,
        arguments: dict[str, object],
        *,
        privilege: DeploymentPrivilege,
        payload: bytes = b"",
        timeout_seconds: int = 60,
    ) -> dict[str, object]:
        if privilege is not self._privilege or privilege is DeploymentPrivilege.INSUFFICIENT:
            raise TrainingNodeDeploymentError(
                "training_node_deployment_account_insufficient",
                "Deployment privilege was not established for this SSH session",
            )
        arguments_json = json.dumps(
            arguments,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if not re.fullmatch(r"[\x20-\x7e]{2,8192}", arguments_json):
            raise ValueError("Deployment operation arguments are unsupported")
        installer_argv = (
            "/usr/bin/python3",
            "-c",
            _REMOTE_SOURCE_LOADER,
            _encoded_remote_source(_REMOTE_INSTALLER),
            operation.value,
            arguments_json,
        )
        stdin = payload
        if privilege is DeploymentPrivilege.SUDO:
            if self._effective_sudo_password is None:
                prefix = ("/usr/bin/sudo", "-n", "--")
                remote_argv = prefix + installer_argv
            else:
                remote_argv = (
                    "/usr/bin/python3",
                    "-c",
                    _REMOTE_SOURCE_LOADER,
                    _encoded_remote_source(_REMOTE_SUDO_BRIDGE),
                    *installer_argv,
                )
                stdin = (
                    self._effective_sudo_password.encode("utf-8")
                    + b"\n"
                    + payload
                )
        else:
            remote_argv = installer_argv
        result = self._session.run_fixed_argv(
            remote_argv,
            stdin_payload=stdin,
            timeout_seconds=timeout_seconds,
            operation_name=f"deploy:{operation.value}",
        )
        if result.return_code != 0:
            raise RuntimeError("fixed remote deployment operation failed")
        try:
            parsed = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("fixed remote deployment response is invalid") from None
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("changed"), bool)
            or set(parsed) - {"changed", "value"}
        ):
            raise RuntimeError("fixed remote deployment response is malformed")
        return parsed


__all__ = ["OpenSshWorkerDeploymentBackend"]
