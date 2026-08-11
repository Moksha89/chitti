# Chitti host runner

The internet-facing `chitti` container never receives Docker access. Install
the deployed application under `/opt/chitti`, install the unit as root on the
host, and provide `/etc/chitti/worker-runner.env` with
the runner-only database URL:

```text
DATABASE_URL=postgresql+asyncpg://chitti_runner:<secret>@127.0.0.1:5432/chitti
```

Apply `runner-role.sql` as the database administrator after replacing the
placeholder password. The runner polls queued rows, claims one run at a time,
observes durable `cancel_requested` events, and appends execution history.
There is no inbound runner API.

The runner requires host root because each workspace is a per-run host `tmpfs`
mounted with the recorded artifact quota, then bind-mounted into disposable
containers. The worker container itself remains non-root and receives no
Docker socket.

`StateDirectory=chitti-worker` provides `/var/lib/chitti-worker`; the runner
uses `/var/lib/chitti-worker/runs`. It must be visible to the host Docker
daemon; do not place it under a private container volume or a
systemd-private `/tmp` path. `ProtectHome=true` is therefore compatible with
the runner. `ProtectSystem=full` leaves the state directory writable through
`ReadWritePaths`, and `NoNewPrivileges=true` is safe because the unit starts
as root and neither Docker CLI access nor the existing mount capabilities
require acquiring new privileges.
