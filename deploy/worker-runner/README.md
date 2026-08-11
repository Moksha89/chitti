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

The disk-fill proof uses a bind mount with the `--mount` key/value form; the
bind is read-write by default. With a mounted proof workspace in
`$workspace`, the worker-side write must fail at the filesystem boundary:

```sh
docker run --rm --network none --read-only --user 65532:65532 \
  --mount "type=bind,src=${workspace},dst=/workspace" \
  chitti-sandbox:latest sh -c \
  'dd if=/dev/zero of=/workspace/fill bs=1M count=64 status=none'
```

The frontend-build policy is intentionally conservative for this no-swap host:

- `2g` memory and `1` CPU: enough for the fixture's Next.js/R3F build while
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

Model coding runs add a second, runner-enforced budget document to the
immutable `worker_runs.limits` JSON:

- 40 model iterations per task and 120 total tool calls;
- 300,000 total model tokens;
- 2 MiB total model-authored writes;
- 1,800 seconds overall run wall-clock, separate from each Docker operation's
  900-second timeout;
- $0.75 loop-side spend cap, with the `coder` and `reviewer` LiteLLM routes
  capped at $5/day and $1/day respectively at the gateway. The loop-side cap
  means one run can never spend more than $0.75, so $0.75 is the worst-case
  cost of one run; daily route caps are an additional bad-day guard.

Only the host runner constructs model prompts and holds the LiteLLM credential.
The worker receives structured fixed tools, never a model key or arbitrary
argv. Model prompts and responses are bounded append-only artifacts, with
token/cost metadata in `worker_model_calls`; reviewer output is stored as a
separate artifact. Older exploratory turns are compacted while the stable
prompt prefix, task contract, recent turns, and build/test feedback remain;
each compaction is recorded as a run event. Approval remains evidence review
only: this slice does not publish, merge, or deploy sandbox output.
