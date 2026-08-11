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
# Set POSTGRES_PASSWORD, LITELLM_MASTER_KEY, and an Argon2id
# CHITTI_PASSWORD_HASH (never put a plaintext password in .env).
make up
make migrate
make test
make lint
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

## Web interface and authentication

Caddy is the only public surface. It publishes the server-rendered Chitti
login and chat UI over HTTPS, while Chitti, LiteLLM, and PostgreSQL remain
loopback-only. Set `DOMAIN` to the hostname used by Caddy. For an IP-only
deployment, an `sslip.io` hostname derived from the server address can be
placed in the server-side `.env`; the concrete hostname must never be
committed.

The single configured user is `akirah`. `CHITTI_PASSWORD_HASH` must contain an
Argon2id hash, never a plaintext password. The first generated password is
forced to change at first login. Sessions are server-side, expire, use
HttpOnly/Secure/SameSite cookies, and require CSRF tokens for state changes.
Repeated login failures trigger a temporary lockout. The dashboard shows live
decisions and unresolved contradictions; choosing a resolution appends a new
decision and supersedes the prior one.

## Telegram

Telegram is retained as an opt-in secondary interface. Set
`TELEGRAM_BOT_TOKEN` and a comma-separated
`ALLOWED_TELEGRAM_USER_IDS`. Messages from every other Telegram user/chat ID
are ignored. The polling loop is inside the Chitti service and stops cleanly.

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

The Chitti, LiteLLM, and PostgreSQL host ports remain loopback-only. Caddy
publishes only ports 80 and 443 and proxies to the authenticated Chitti app.

The Caddy image is built with the pinned `mholt/caddy-ratelimit` v0.1.0
module because the stock Caddy image has no rate-limit directive. Login POSTs
are limited at the proxy to five requests per client address per minute before
they reach Chitti. Caddy normalizes `X-Forwarded-For`, and Chitti only honors
that header from its fixed Caddy container address.

Sessions and login lockout buckets are intentionally process-local in Phase 1.
A Chitti restart invalidates sessions and clears in-memory lockout state; this
is an operational behavior, not durable authentication state.

## Later-phase seams

`ModelProvider`, `ProjectState`, and the `WorkerDispatcher` protocol are
explicit interfaces. Phase 1 never invokes a worker; a future dispatcher can
be attached without making the chat endpoint write code.
