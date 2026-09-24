# ai-service (`soc-ai`)

Stateless prompt → text service in front of Ollama.

It owns **no database, no queue, no job state and no users**. It takes a fully
rendered prompt and returns the model's text. Job rows, retries, API keys and
the audit trail live in the core backend (`../backend`, NestJS), which is the
only thing that writes to Postgres.

```
soc-core-worker --POST /analyze--> soc-ai --POST /api/generate--> soc-ollama
```

## Contract

Mirrored by `AiGenerateResult` in
`../backend/src/modules/ai/ai.service.ts`. Both ends of one HTTP call, so a
change on either side is a change on both.

```
POST /analyze
  request   { "rendered_prompt": "<instruction + raw log>", "model": "qwen2.5:7b" }
  response  { "response": "...", "model": "qwen2.5:7b",
              "total_duration_ms": 12345, "tokens_prompt": 900, "tokens_eval": 210 }

GET /health -> { "status": "ok", "app": "...", "model": "...", "ollama": true }
```

Failure maps to status, because the backend treats them differently:

| Condition | Status |
| --- | --- |
| Ollama exceeded `OLLAMA_TIMEOUT` | `504` — worth retrying |
| Ollama unreachable | `503` |
| `rendered_prompt` empty | `422` |

## The prompt is rendered upstream

This service does not assemble prompts. `buildRenderedPrompt` in the backend
joins the SOAR playbook's instruction to the raw log, and stores the result on
the job row as `rendered_prompt`. Keeping assembly in one place is what lets an
answer be reproduced from the row months later — a second renderer here would
mean the stored prompt was not necessarily the prompt that ran.

## Files

| File | Role |
| --- | --- |
| `main.py` | The two routes and their error mapping. |
| `services/ollama_service.py` | The Ollama HTTP call. |
| `schemas/analysis.py` | Request and response models — the backend contract. |
| `core/config.py` | Env to typed settings. |

## Configuration

Read from the root `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | `http://soc-ollama:11434` | |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Default when the request names none. |
| `OLLAMA_TIMEOUT` | `120` | Seconds for one generation. |

There is deliberately no `DATABASE_URL`, `JWT_SECRET_KEY` or admin bootstrap
here. A setting that exists is a setting someone will eventually wire up.

## Commands

```bash
docker compose up -d --build
docker compose logs -f soc-ai
curl -s localhost:8100/health
curl -s -X POST localhost:8100/analyze \
  -H 'Content-Type: application/json' \
  -d '{"rendered_prompt":"Say OK and nothing else."}'
```

## Next: the MCP client

The connectors under `../mcp` publish tools but nothing consumes them yet. The
agent loop — discover tools over MCP, offer them to the model, run what it asks
for, feed results back — belongs here, in the same process as the Ollama call,
so a loop iteration does not cross the network twice.

That needs `/api/chat` rather than `/api/generate`: tool schemas travel in the
`tools` parameter, which `/api/generate` has no place for.

## Tooling

`api-tests/` is a Bruno collection for this service: `/analyze`, `/llm-probe`
and `/health`. The probe is the useful one when an answer looks wrong — it
shows the request that was sent and the raw events that came back, which is how
you tell "the LLM server said something unexpected" from "we parsed it wrong".

## Running the whole stack

This repo runs on its own with the compose file above, which is enough to work
on it. Running the SOC end to end takes all five repos, checked out side by
side, and one script that writes the `.env` files:

```bash
git clone https://gitlab.com/agentic-soc/backend.git     Backend
git clone https://gitlab.com/agentic-soc/frontend.git    Frontend
git clone https://gitlab.com/agentic-soc/ai-service.git  AI-Service
git clone https://gitlab.com/agentic-soc/llm-server.git  LLM-Server
git clone https://gitlab.com/agentic-soc/mcp-server.git  MCP-Server

cd Backend
./deploy/dev-setup.sh                            # writes .env in all four
docker compose -f deploy/dev.yml up -d --build   # http://localhost:3001
```

The script lives in Backend because three of the secrets it generates have to
be identical in more than one repo — the tokens the connectors check — and
they are the ones that break quietly: a mismatch is a 401 with nothing saying
why. Generating them in one place is the whole point.

---

## Where this came from

Split out of the SOC-AI monorepo. The other pieces:

| Repo | What it is |
| --- | --- |
| `Backend` | API, queue worker, Postgres, Redis. The only writer to the database. |
| `AI-Service` | The backend's stateless door to the agent platform. |
| `LLM-Server` | Agents as data — model, prompt, tools — and the loop that runs them. |
| `MCP-Server` | Tool servers for Splunk, ThreatQ and Confluence. |
| `Frontend` | The analyst dashboard. |

They meet on one Docker network called `soc-network`, created by whichever
stack comes up first, and address each other by container name. Bring up
`MCP-Server` and `LLM-Server` first: those are the ones the rest call.

Each repo carries only the settings its own services read. In the monorepo one
`.env` was shared by every container, which meant the Splunk connector's
environment also held the database password and the ThreatQ credentials — a
compromise of any one service read the secrets of all of them.
