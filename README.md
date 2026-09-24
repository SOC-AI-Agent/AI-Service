# ai-service (`soc-ai`)

Two jobs, both for the core backend and nobody else:

1. **The door to the agent platform.** It takes a fully rendered prompt, asks
   the LLM server to run a named agent on it, reads the answer off the stream
   and returns it.
2. **The playbook index.** The backend sends it the Confluence pages; it
   extracts each playbook's fields, embeds them, and answers "which playbook
   covers this alert".

It owns **no database, no queue, no job state and no users**. Job rows,
retries, API keys and the audit trail live in the core backend
(`../Backend`, NestJS), the only thing that writes to Postgres. The one thing
kept here is the playbook index, a single file on the `playbook-index` volume.

```
soc-core-worker ──POST /analyze──────────► soc-ai ──POST /agents/{agent}/run──► soc-llm-server
soc-core-worker ──PUT  /playbooks/index──► soc-ai      (pages read from soc-mcp-confluence GET /pages)
soc-core-worker ──POST /playbooks/match──► soc-ai
```

## Access

No published port: only the backend calls this, over `soc-network`. Every
route except `/health` wants `Authorization: Bearer $AI_SERVICE_AUTH_TOKEN` —
the same value in `Backend/.env` and here — and the service refuses to start
without one. `/docs` and `/openapi.json` are behind it too.

## Contract

Mirrored by `AiService`, `PlaybookService` and `PlaybookSyncService` in
`../Backend/src/modules`. Both ends of one HTTP call, so a change on either side
is a change on both.

```
POST /analyze            {rendered_prompt, agent?, model?}
                      -> {response, model, total_duration_ms, tokens_prompt, tokens_eval, run_id, ...}
POST /llm-probe          same body; returns the request sent and the raw stream lines

PUT  /playbooks/index    {source, pages: [{id, title, version, url, body}], allow_empty?}
                      -> {playbooks, pages, skipped, conflicts, embedded, reused, took_ms, semantic}
POST /playbooks/match    {alert_name}
                      -> {matched, how, matched_name, matched_field?, similarity?, playbook, note?, corpus}
GET  /playbooks          what is indexed, and whether matching by meaning works

GET  /health             public
```

## The prompt is rendered upstream

This service does not assemble prompts. `buildRenderedPrompt` in the backend
joins the raw log to the context gathered for it (ThreatQ, the matched
playbook), and stores the result on the job row as `rendered_prompt`. The
instruction is the agent's own system prompt on the LLM server. Keeping assembly in one place is what lets an
answer be reproduced from the row months later — a second renderer here would
mean the stored prompt was not necessarily the prompt that ran.

## The playbook index

**Extraction.** A playbook is a page with a Page Properties table.
`playbooks/extract.py` reads the two-column rows, maps the labels
(`FIELD_LABELS`), and reduces each cell to its text — the Confluence connector
used to keep a cell's XHTML, so a name the editor wrapped in `<p>` never matched
exactly. A page with no alert name is not a playbook and is listed under
`skipped`. `splunk_query` must be JSON (parameters, never SPL); a code macro's
CDATA is read verbatim.

**Matching**, cheapest first, each tier only when the one before cannot answer:

| Tier | `how` | Handles | Cost |
| --- | --- | --- | --- |
| 1 | `exact` | the name or an alias, case and spacing folded | ~0 ms |
| 2 | `fuzzy` | a renamed detection, by spelling (`PLAYBOOK_FUZZY_CUTOFF`) | ~0 ms |
| 3 | `semantic` | different words for the same detection | ~5 ms, first query per name only |

Anything else is `miss` (with how close the nearest came), and `unindexed`
until the backend's first sync. A wrong playbook is worse than none — the
analyst reads criteria written for another detection — so tier 3 answers only
when the best playbook clears `PLAYBOOK_SEMANTIC_MIN_SCORE` **and** leads the
next one by `PLAYBOOK_SEMANTIC_MIN_MARGIN`.

**What is embedded:** the rule description and the true and false conditions —
not the name. The query is always an alert name, and names are templated: on
the name alone, "Excessive DNS Failures" scored 0.804 against "Excessive Failed
Logins", higher than most genuine renames. Names are what tiers 1 and 2 are for.

**Calibration**, `bge-small-en-v1.5` on the notebook's six sample playbooks
(`../MCP-Server/notebooks/playbook_rag.ipynb`) and 26 alert names:

| | matched correctly | matched wrongly |
| --- | --- | --- |
| 12 renamed detections | 8 (the other 4 answer `miss`) | 0 |
| 14 alerts with no playbook | — | 0 |

`bge-base-en-v1.5` did no better. Six playbooks is not the real wiki: rerun the
calibration on the real pages before relying on tier 3.

**Embedding only what changed.** Each embedded field carries a digest of its
text. A sync reuses the vector of every field whose text and model are
unchanged, so a sync where nothing moved embeds nothing (measured: 18 fields
embedded in 0.27 s the first time, 0 in 3 ms the second), and an edited page
costs its changed fields only. Query vectors are cached per alert name.

**The model** runs in this process on CPU, never through Ollama: soc-ollama
holds one model at a time, and an embedding call there would evict the agents'
7B. It is baked into the image at build time and the hub is switched off at
runtime (`HF_HUB_OFFLINE=1`). Every encode runs in a worker thread, so a sync
never stalls an `/analyze` that is streaming. Loading takes ~10 s and is done in
the background at startup. If the model cannot be loaded, tiers 1 and 2 carry
on and `/playbooks` says why tier 3 is off.

**Refusals.** An index request that would replace a non-empty index with zero
playbooks is a `409` unless it says `allow_empty: true` — a sync that finds
nothing is far more often a broken read than an emptied wiki. More than
`PLAYBOOK_MAX_PAGES` pages is a `422`. The index file is written to a temporary
name and renamed over the old one.

## Files

| File | Role |
| --- | --- |
| `main.py` | `/analyze`, `/llm-probe`, `/health`; startup loads the index and warms the model. |
| `core/auth.py` | The bearer gate. |
| `core/config.py` | Env to typed settings. |
| `services/llm_client.py`, `services/sse.py` | The LLM server call and its stream reader. |
| `playbooks/extract.py` | Page Properties -> playbook records. |
| `playbooks/router.py` | Tiers 1 and 2. |
| `playbooks/embedder.py` | The embedding model, loaded on first use. |
| `playbooks/store.py` | The index: build, reuse, save, load, tier 3. |
| `playbooks/routes.py` | `/playbooks`. |
| `tests/` | pytest, with a stand-in embedder: no torch, no download. |

## Configuration

See `.env.example`; each setting is explained there. The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `AI_SERVICE_AUTH_TOKEN` | — | Required. Same value as `Backend/.env`. |
| `LLM_SERVER_URL` | `http://soc-llm-server:8400` | |
| `PLAYBOOK_INDEX_PATH` | `/data/playbook-index.npz` | On the `playbook-index` volume. |
| `PLAYBOOK_SEMANTIC` | `true` | `false` = tiers 1 and 2 only, no model loaded. |
| `PLAYBOOK_SEMANTIC_MIN_SCORE` / `_MIN_MARGIN` | `0.75` / `0.05` | See calibration above. |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Must be the model the image was built with (`--build-arg EMBED_MODEL=`). |

## Commands

```bash
docker compose up -d --build        # the build needs download.pytorch.org and huggingface.co
docker compose logs -f soc-ai

# No published port: ask from inside the container.
docker exec soc-ai python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8100/health').read())"

# Tests: no model and no LLM server needed.
pip install -r requirements.txt -r tests/requirements.txt
pytest -q
```

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
| `AI-Service` | The backend's door to the agent platform, and the playbook index. |
| `LLM-Server` | Agents as data — model, prompt, tools — and the loop that runs them. |
| `MCP-Server` | Connectors for Splunk, ThreatQ and Confluence. |
| `Frontend` | The analyst dashboard. |

They meet on one Docker network called `soc-network`, created by whichever
stack comes up first, and address each other by container name. Bring up
`MCP-Server` and `LLM-Server` first: those are the ones the rest call.

Each repo carries only the settings its own services read. In the monorepo one
`.env` was shared by every container, which meant the Splunk connector's
environment also held the database password and the ThreatQ credentials — a
compromise of any one service read the secrets of all of them.
