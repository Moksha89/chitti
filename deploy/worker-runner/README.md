# Chitti host runner

The internet-facing `chitti` container never receives Docker access. Install
the deployed application under `/opt/chitti`, install the unit as root on the
host, and provide `/etc/chitti/worker-runner.env` with
the runner-only database URL:

```text
DATABASE_URL=postgresql+asyncpg://chitti_runner:<secret>@127.0.0.1:5432/chitti
LITELLM_BASE_URL=http://127.0.0.1:4000
LITELLM_MASTER_KEY=<from the application environment>
```

## Repeatable deployment

From the application checkout, run `deploy/deploy-main.sh` as root. The
command fetches `main`, starts the stack so its normal startup migration path
runs, builds `chitti-sandbox:latest`, installs this unit, provisions the
runner-only database role on first use, and verifies the schema and container
boundaries. Re-running it preserves the existing runner environment file and
does not rotate that role's password.

The deployment recreates the LiteLLM gateway and verifies its authenticated
loaded model routes after startup. If `litellm/config.yaml` changes, deploy
through this script so the gateway reloads the checked-out configuration.

The script's schema, role-privilege, Docker-socket, and isolated-worker
network checks run on the host. The privileged ext4 mount, exact quota and
disk-fill proofs, cancellation cleanup, restart recovery, and a real queued
worker run still require the deployed host; CI intentionally does not claim
those Docker, browser, or privileged-system checks.

Apply `runner-role.sql` as the database administrator after replacing the
placeholder password. The runner polls queued rows, claims one run at a time,
observes durable `cancel_requested` events, and appends execution history.
There is no inbound runner API.

Deployment restarts the runner after installing the application and verifies a
loaded-code identity emitted by that process. The identity includes the
running PID and a digest of the imported runner, worker, provider, and tool
modules; deployment refuses to finish if it does not match the checked-out
code.

The digest hashes the source files resolved from those imported modules when
the identity record is written; it is not a hash of already-executed bytecode.
The runner writes the record as the first operation in `run_forever`, after
module import and before database setup. This leaves only the small interval
between import and record creation in which a source replacement could differ
from the bytes already loaded by the interpreter. The PID check and removal of
any stale identity record still require that this record come from the process
started by the deployment.

The runner requires host root because each workspace is a per-run ext4
filesystem backed by a file under `/var/lib/chitti-worker/runs`. The backing
file is mounted with a loop device and its exact size is the recorded workspace
quota, then bind-mounted into disposable containers. This keeps large
`node_modules` trees on disk instead of consuming host RAM on this no-swap box.
The worker container itself remains non-root and receives no Docker socket.

Published previews are stored under `/var/lib/chitti-previews`, which is the
only preview directory bind-mounted read-only into the application container.
Unapproved export staging is stored separately under
`/var/lib/chitti-preview-staging`; only the host runner can read and write that
path. The application serves a preview only when its identifier is present in
the database's unexpired `previews` table.

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

The disk-fill and orphan proofs must use a workspace created by the
dispatcher. Do not call `mount`, `losetup`, or `mkfs` from the proof shell:
that would bypass the runner's host-namespace path and prove the wrong thing.
Enqueue a disposable fixed-operation run through the authenticated
`POST /plans/{revision_id}/runs` route, wait until its workspace is visible
from the host with `findmnt`, and set `$workspace` to that dispatcher-created
path. The runner must verify the host mount and the worker-visible mount
before it starts the operation. While that workspace is still active, the
worker-side write must fail at the filesystem boundary:

```sh
docker run --rm --network none --read-only --user 65532:65532 \
  --mount "type=bind,src=${workspace},dst=/workspace" \
  chitti-sandbox:latest sh -c \
  'dd if=/dev/zero of=/workspace/fill bs=1M count=64 status=none'
```

Record a non-zero exit status, the host `df` result, and a PostgreSQL health
check. Let the dispatcher finish and verify that its normal cleanup removes
the mount, loop device, image, workspace, and worker container.

For orphan recovery, enqueue another disposable fixed-operation run, stop the
runner after its dispatcher-created mount is visible, and start the runner
again. Verify that reconciliation removes the stale worker container only
after the host mount and loop device are gone. A deliberately uncleanable
candidate must produce a durable failed run event or a loud journal report;
the proof must never delete its backing image while a mount or loop survives.
Finish both proofs with a host-wide leak sweep for dispatcher proof run IDs.

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
The runner uses the existing gateway credential because the deployed LiteLLM
configuration budgets the `coder` and `reviewer` routes, plus global and
provider limits; it does not configure separate virtual-key budgets. The
deployment refreshes the root-only runner copy whenever the application
credential changes. The runner must use the host-loopback URL above, not the
compose-only `litellm` hostname.
The worker receives structured fixed tools, never a model key or arbitrary
argv. Model prompts and responses are bounded append-only artifacts, with
token/cost metadata in `worker_model_calls`; reviewer output is stored as a
separate artifact. Older exploratory turns are compacted while the stable
prompt prefix, task contract, recent turns, and build/test feedback remain;
each compaction is recorded as a run event. Approval remains evidence review
only: this slice does not publish, merge, or deploy sandbox output.

Plan approval records preserve the optional reason text and display it in the
plan view. That reason is the only attribution available; the schema does not
record a separate approver identity, so a blank reason is not evidence of an
owner click.
