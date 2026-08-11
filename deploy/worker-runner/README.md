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

The runner requires host root because each workspace is a per-run ext4
filesystem backed by a file under `/var/lib/chitti-worker/runs`. The backing
file is mounted with a loop device and its exact size is the recorded workspace
quota, then bind-mounted into disposable containers. This keeps large
`node_modules` trees on disk instead of consuming host RAM on this no-swap box.
The worker container itself remains non-root and receives no Docker socket.

`StateDirectory=chitti-worker` provides `/var/lib/chitti-worker`; the runner
uses `/var/lib/chitti-worker/runs`. It must be visible to the host Docker
daemon; do not place it under a private container volume or a
systemd-private `/tmp` path. `ProtectHome=true` is therefore compatible with
the runner. `ProtectSystem=full` leaves the state directory writable through
`ReadWritePaths`, and `NoNewPrivileges=true` is safe because the unit starts
as root and neither Docker CLI access nor the existing mount capabilities
require acquiring new privileges.

The runner removes each mounted filesystem and backing file in its normal
cleanup path. On startup it also unmounts and removes stale `chitti-run-*.img`
files left by a crash before claiming new work. This recovery sweep is
necessary because a leaked quota image would eventually consume the host disk.

The frontend-build policy is intentionally conservative for this no-swap host:

- `2g` memory and `2` CPUs: enough for the fixture's Next.js/R3F build while
  leaving headroom for PostgreSQL, Redis, LiteLLM, Caddy, and Chitti.
- `4 GiB` disk-backed workspace: enough for the checked-in dependency tree and
  `.next` output without putting build pages in RAM.
- `900` seconds wall-clock: accommodates package installation and browser
  evidence without permitting an unattended run to occupy the single slot
  indefinitely.
- `512` PIDs and `1024` file descriptors: higher than the original cage for
  Node's process and module fan-out, but still bounded.
- `256 MiB` browser shared memory: enough for Chromium's preview renderer
  without exposing an unconstrained `/dev/shm`.

The workspace filesystem is ext4 backed by a sparse per-run file. Ext4's
filesystem limit makes writes fail inside the worker at the quota boundary;
the runner's post-operation size check is only an audit guard, not the quota
mechanism.
