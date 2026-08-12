# Training Worker v1 safety boundary

Training Worker v1 is a node-local, read-only inventory daemon. It persists a
private worker identity, collects host/GPU resources, and keeps a local SQLite
ledger for conservative restart reconciliation.

It deliberately does **not**:

- accept jobs or poll for executable work;
- invoke a shell or run user-provided commands;
- start, stop, signal, or otherwise manage training processes;
- inspect or modify a model project;
- reserve GPUs.

GPU discovery first uses optional NVML bindings and then a single fixed
`nvidia-smi --query-gpu=... --format=csv,noheader,nounits` argv. The fallback
uses `subprocess.run(..., shell=False)` with a five-second timeout. No input can
alter its executable or arguments.

The Web node page performs installation through one explicitly authorised SSH
deployment. The fixed installer requires either a root login or working sudo,
creates the non-login system account `datapilot-worker`, installs versioned
artifacts below `/opt/datapilot-training-worker`, creates private state below
`/var/lib/datapilot-training-worker`, writes non-secret configuration below
`/etc/datapilot-training-worker`, and enables the system service shown here.
The user does not create the Worker account or enable systemd linger manually.

If the SSH deployment account is neither root nor allowed to use sudo, the
installer performs no privileged writes and returns the stable error
`training_node_deployment_account_insufficient` (shown in the UI as
“部署账号权限不足”). It never falls back to running the daemon as the SSH login
account.

First enrollment uses a one-time token generated internally by the control plane:

```console
python3 /opt/datapilot-training-worker/current/datapilot-training-worker.pyz \
  --state-dir /var/lib/datapilot-training-worker \
  --center-base-url https://datapilot.example.internal \
  --enrollment-token-stdin \
  --once
```

The fixed installer supplies the token on stdin while running as
`datapilot-worker`. The CLI deliberately does not accept it as an argv value,
environment variable, or file path, so it does not enter shell history, process
listings, or persistent configuration.

The returned bearer credential is saved only as `worker-token` in the private
state directory with mode `0600`; the enrollment token is not persisted. The
installer writes `/etc/datapilot-training-worker/worker.env` with mode `0640`,
owned by `root:datapilot-worker`, containing only the non-secret fixed configuration:

```dotenv
DATAPILOT_CENTER_BASE_URL=https://datapilot.example.internal
DATAPILOT_NODE_REF=node_...
```

The worker can also be inspected offline without installing a service:

```console
python -m vla_data_juicer_agents.training_worker --state-dir /tmp/worker-state --once
```

The center transport is limited to a fixed HTTP(S) origin, one enrollment
endpoint, and one bearer-authenticated heartbeat endpoint. It rejects redirects
and applies bounded request/response limits. Any process executor requires a
separate security review.
