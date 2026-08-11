# Chitti — Phase 1

Phase 1 is deliberately small: Chitti talks and Chitti remembers. It does not
write code, run Playwright, generate images, or dispatch worker agents yet.
The provider and project-state interfaces leave seams for those later phases.

## Model and embedding decisions

LiteLLM is the only model gateway. The Z.AI GLM Coding Plan documentation
specifies the OpenAI-compatible base URL
`https://api.z.ai/api/coding/paas/v4` and model `glm-5.2`:

- [Z.AI GLM Coding Plan quick start](https://docs.z.ai/devpack/quick-start)
- [Z.AI GLM-5.2 model selection](https://docs.z.ai/devpack/latest-model)
- [Z.AI chat completion reference](https://docs.z.ai/api-reference/llm/chat-completion)

The Anthropic-compatible endpoint is `https://api.z.ai/api/anthropic`, but
Chitti uses LiteLLM's OpenAI-compatible gateway, so the coding endpoint is
configured. Z.AI says Coding Plan access is restricted to officially supported
tools/products; verify the intended account entitlement before production use.

DeepSeek is configured with `https://api.deepseek.com` and the documented
OpenAI-compatible model name `deepseek-v4-flash` as the bulk fallback. The role
aliases (`chitti-chat`, `planner`, `coder`, `bulk`) intentionally hide vendor
names from Chitti.

DeepSeek references:

- [DeepSeek first API call](https://api-docs.deepseek.com/)
- [DeepSeek chat completion](https://api-docs.deepseek.com/api/create-chat-completion)

Neither the Z.AI Coding Plan documentation nor DeepSeek's API documentation
provides a usable embeddings model/endpoint for this gateway configuration.
Semantic recall therefore uses the local ONNX-backed FastEmbed implementation
with the `sentence-transformers/all-MiniLM-L6-v2` model inside the Chitti
container (384 dimensions). FastEmbed avoids the full Torch/CUDA dependency
tree. The first embedding request may download the model; for a strictly
offline deployment, bake the model into the image or mount a local FastEmbed
cache.

LiteLLM is pinned to `ghcr.io/berriai/litellm:v1.80.0-stable.1`. The config
uses the current `model_list`, `litellm_settings`, `router_settings`, and
`general_settings` schema. Request logs go to stdout; a daily provider budget
and per-deployment budget are configured in `litellm/config.yaml`.

## Run locally

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD and LITELLM_MASTER_KEY to local dummy values.
make up
make migrate
make test
make lint
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'content-type: application/json' \
  -d '{"message":"I prefer a dark, minimal interface.","project":"demo"}'
```

With empty provider keys, the gateway will reject real model requests. Tests
and the local fake-provider path do not require real credentials:

```bash
CHITTI_PROVIDER=fake pytest -q
```

## Memory tiers

1. `profile/PROFILE.md` is loaded verbatim into every system prompt and is
   never edited by the application.
2. `decisions` is append-only. A database trigger permits only setting
   `superseded_by`; application code has no update/delete methods.
3. `memory_chunks` stores 384-dimensional local embeddings and metadata for
   similarity recall using pgvector.
4. `ProjectState` reads and writes `plan.md`, `tasks.md`,
   `architecture.md`, and `open_questions.md` under a target repo's
   `.chitti/` directory.

After every turn, Chitti runs the memory extraction pass. It deduplicates
facts, detects contradictions against active decisions, and asks the user to
resolve a contradiction instead of silently overwriting history. This path is
covered by tests.

## Telegram

Set `TELEGRAM_BOT_TOKEN` and a comma-separated
`ALLOWED_TELEGRAM_USER_IDS`. Messages from every other Telegram user/chat ID
are ignored. The polling loop is inside the Chitti service and stops cleanly.
The local `/chat` endpoint is the deterministic test surface.

## Deployment

`deploy/bootstrap.sh` is an idempotent Ubuntu 24.04 bootstrap. It creates a
non-root service user, installs Docker/UFW/fail2ban, configures key-only SSH
only after verifying an authorized key, allows only 22/80/443, and installs a
nightly encrypted local `pg_dump` rotation job using `age`. Set
`BACKUP_AGE_RECIPIENT` to a public age recipient in the cron environment. To
restore a backup, use the matching private identity:

```bash
age --decrypt --identity /root/.config/age/keys.txt \
  --output chitti.dump backups/chitti-<timestamp>.dump.enc
docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" < chitti.dump
```

Review it before running on a real VPS. It intentionally refuses to disable
password authentication when no authorized key is present.

Phase 1 deliberately does not publish Caddy ports or proxy Chitti. Use
Telegram or an SSH tunnel such as
`ssh -L 8000:127.0.0.1:8000 administrator@host`. The Caddy service and its
disabled preview-site template remain as a later-phase seam. Wildcard
`*.dev.<DOMAIN>` certificates require DNS-01 credentials and a Caddy image
with the matching DNS provider module; HTTP-01 cannot issue them.

## Later-phase seams

`ModelProvider`, `ProjectState`, and the `WorkerDispatcher` protocol are
explicit interfaces. Phase 1 never invokes a worker; a future dispatcher can
be attached without making the chat endpoint write code.
