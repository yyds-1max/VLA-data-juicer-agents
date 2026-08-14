from __future__ import annotations

import asyncio
import math
import random
from time import monotonic
from uuid import uuid4

from .errors import TrainingConflictError
from .store import TrainingStore


class TrainingWorker:
    def __init__(
        self,
        store: TrainingStore,
        *,
        tick_seconds: float = 0.25,
        worker_id: str | None = None,
        recovery_interval_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.tick_seconds = max(0.01, tick_seconds)
        self.worker_id = worker_id or f"fake-worker-{uuid4().hex}"
        self.recovery_interval_seconds = max(0.01, recovery_interval_seconds)
        self._next_recovery_at = 0.0
        self._stop = asyncio.Event()

    async def _recover_stale_if_due(self, *, force: bool = False) -> None:
        current = monotonic()
        if not force and current < self._next_recovery_at:
            return
        await asyncio.to_thread(self.store.recover_stale_runs)
        self._next_recovery_at = monotonic() + self.recovery_interval_seconds

    async def run_forever(self) -> None:
        await self._recover_stale_if_due(force=True)
        while not self._stop.is_set():
            await self._recover_stale_if_due()
            run = await asyncio.to_thread(self.store.claim_next_run, self.worker_id)
            if run is None:
                try: await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
                except TimeoutError: pass
                continue
            await self._simulate(run)

    async def stop(self) -> None:
        self._stop.set()

    async def _simulate(self, run: dict) -> None:
        run_ref = run["run_ref"]
        try:
            while True:
                run = await asyncio.to_thread(
                    self.store.transition_running, run_ref, self.worker_id
                )
                if run["status"] == "cancelled":
                    return
                stage = next(
                    (item for item in run["stages"] if item["status"] == "running"),
                    None,
                )
                if stage is None:
                    return
                stage_ref = stage["stage_ref"]
                rng = random.Random(f"{run_ref}:{stage_ref}")
                started = monotonic()
                total = int(stage["total_steps"])
                for step in range(1, total + 1):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(self.tick_seconds)
                    await self._recover_stale_if_due()
                    progress = step / total
                    loss = max(
                        0.015,
                        2.4 * math.exp(-3.2 * progress)
                        + rng.uniform(-0.035, 0.035),
                    )
                    learning_rate = 2e-4 * max(
                        0.02, (1 + math.cos(math.pi * progress)) / 2
                    )
                    metric = {
                        "step": step,
                        "total_steps": total,
                        "epoch": round(progress * 3, 4),
                        "loss": round(loss, 6),
                        "learning_rate": learning_rate,
                        "grad_norm": round(0.7 + rng.random() * 0.8, 5),
                        "elapsed_seconds": round(monotonic() - started, 3),
                        "gpus": [
                            {
                                "uuid": gpu,
                                "utilization_percent": round(
                                    62 + rng.random() * 26, 1
                                ),
                                "memory_used_mib": round(
                                    32000 + rng.random() * 18000
                                ),
                            }
                            for gpu in run["gpu_uuids"]
                        ],
                    }
                    run = await asyncio.to_thread(
                        self.store.append_step,
                        run_ref,
                        self.worker_id,
                        metric,
                        f"{stage['stage_name']} step {step}/{total} "
                        f"loss={loss:.6f} lr={learning_rate:.8f}",
                        stage_ref,
                    )
                    if run["status"] == "cancelled":
                        return
                    if (
                        stage.get("parameters", {}).get("simulate_failure") is True
                        and step >= max(1, total // 2)
                    ):
                        await asyncio.to_thread(
                            self.store.finish_stage,
                            run_ref,
                            self.worker_id,
                            stage_ref,
                            "failed",
                            "simulated_failure",
                            "The stage reached its configured simulated failure point.",
                        )
                        return
                run = await asyncio.to_thread(
                    self.store.finish_stage,
                    run_ref,
                    self.worker_id,
                    stage_ref,
                    "succeeded",
                )
                if run["status"] in {"succeeded", "failed", "cancelled", "lost"}:
                    return
        except TrainingConflictError:
            return
        except Exception as exc:
            try: await asyncio.to_thread(self.store.finish_run, run_ref, self.worker_id, "failed", "simulation_failed", "The simulation worker failed.")
            except Exception: pass


FakeTrainingWorker = TrainingWorker
