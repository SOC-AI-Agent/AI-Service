# ai-service (`soc-ai`)

Two jobs, both for the core backend and nobody else:

1. **The door to the agent platform.** It takes a fully rendered prompt, asks
   [Agent-Server](../Agent-Server/readme.md) (DALL-M) to run a named agent on
   it, reads the answer off the stream and returns it.
2. **The playbook index.** The backend sends it the Confluence pages; it
   extracts each playbook's fields, embeds them, and answers "which playbook
   covers this alert".

It owns **no database, no queue, no job state and no users**. Job rows,
retries, API keys and the audit trail live in the core backend
(`../Backend`, NestJS), the only thing that writes to Postgres. The one thing
kept here is the playbook index, a single file on the `playbook-index` volume.

```
soc-core-worker ──POST /analyze──────────► soc-ai ──POST /v1/agents/{slug}/chat──► Agent-Server
soc-core-worker ──PUT  /playbooks/index──► soc-ai      (pages the worker read from Confluence's REST API)
soc-core-worker ──POST /playbooks/match──► soc-ai
```

Agent-Server is a separate system — its own repo, own Postgres, own compose —
not a container on `soc-network`. It is reached by URL (`LLM_SERVER_URL`), the
same way Splunk, ThreatQ and Confluence are: see "Agent-Server" below.

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

**The model** runs in this process on CPU, not on Agent-Server's llama.cpp:
that engine only ever answers a chat completion, has no embedding endpoint,
and is a separate system this service cannot reach for anything but
`/v1/agents/.../chat` anyway — running a small model here is the only option,
not a choice against a shared one. It is baked into the image at build time
and the hub is switched off at
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

## Agent-Server

The agent platform this service calls is
[Agent-Server](../Agent-Server/readme.md) (internal name DALL-M) — a separate
repo with its own Postgres, its own `docker compose`, and its own network
(`dall-m-net`). It replaced an earlier in-house LLM server; nothing here
changed shape to fit it, because the point of `LLM_SERVER_URL` /
`LLM_RUN_PATH` / `LLM_INPUT_FIELD` etc. was always to adapt to whichever
platform is configured, and Agent-Server is just the platform that is
configured now.

```
POST {LLM_SERVER_URL}/v1/agents/{slug}/chat   {"message": "...", "stream": true}
  streaming  -> event: token (data: {"content": "..."}), then event: done
  non-stream -> {agent, prompt_version, content, input_tokens, output_tokens,
                 total_tokens, latency_ms}
GET  {LLM_SERVER_URL}/                        -> {"status": "ok"}   (its /health)
```

Not on `soc-network`: reached by URL, the same as Splunk/ThreatQ/Confluence.
`host.docker.internal` (the default) reaches its published port 8000 when it
runs via its own compose on this same machine; point `LLM_SERVER_URL` at its
real host otherwise. It takes no bearer token on `/v1/agents/...` yet (an
`api_client_id` column is reserved for that — see its readme's "Known
limitations") — set `LLM_AUTH_TOKEN` here the day it does.

**Agents are data in Agent-Server's own Postgres, not files in this repo.**
Create `soc-suggest`, `soc-requirements` and `soc-conclude` (or whatever
`LLM_SUGGEST_AGENT` / `LLM_REQUIREMENTS_AGENT` / `LLM_CONCLUDE_AGENT` in
`Backend/.env` name) at its admin UI or `POST /restful/agents` — nothing on
this side creates or restores them.

**No run id comes back.** Agent-Server's response has nothing like the old
`run_id`, so a job's `agent_run_id` is always empty now, and the dashboard's
"view trace" link (which only ever showed when one was set) stops appearing —
not a bug on either side, just a capability the new platform doesn't have yet.

**On a first boot, Agent-Server is down for minutes.** `dall-m-server` waits
for `dall-m-llamacpp` to be healthy (the llama.cpp image's own healthcheck),
which is after its model is downloaded and loaded. Until then `/health` here
reports `"llm_server": false`, and that is not a problem on this side of the
URL.

### In production

Both repos deploy from `main` onto the same machine: each `deploy_production`
job runs on the `soc-deploy` runner, and that runner is the deployment target.
So the default URL holds in production too — `host.docker.internal` reaches
Agent-Server's published port 8000, and the `extra_hosts: host-gateway` line in
this repo's `docker-compose.yml` is what makes that name resolve on Linux.

**1. Deploy Agent-Server first.** Its pipeline builds the stack, runs
`create_db.py` (safe on every deploy: schema plus in-place upgrades) and
waits for `dall-m-server` to answer. Set these as CI variables in that repo —
a key left unset silently takes the development default from its `.env.example`:

| Variable | Why |
| --- | --- |
| `JWT_SECRET` | The default is in the repo; with it anyone can sign an admin token. |
| `DB_PASSWORD` | The default is in the repo, and Postgres is published on 5497 (see step 5). |
| `NEXT_PUBLIC_BASE_URL` | Where the analyst's browser reaches port 8000. Left as `localhost`, the admin UI loads and every call fails. |
| `AGENT_ADMIN_PASSWORD` | First deploy only — it creates the admin. Unset it afterwards: while set, every deploy sets the admin password back to it. |

**2. Create the agents.** Nothing creates them — add them at the admin UI
(`:7100`) or `POST /restful/agents`, with slugs matching the Backend's
`LLM_SUGGEST_AGENT` / `LLM_REQUIREMENTS_AGENT` / `LLM_CONCLUDE_AGENT`:

| Agent | Needed when |
| --- | --- |
| `soc-suggest` | Always — stage 1. |
| `soc-requirements`, `soc-conclude` | Only if the Splunk stage runs (`SPLUNK_LOOKUP_MODE` `manual` or `auto`). With `off`, stage 1 calls `soc-suggest` alone. |

Each must exist, be active and have an active prompt version, or the job
fails: a missing slug comes back as "the LLM server has no agent …", an
inactive agent or one with no active prompt as "LLM server returned 409: …".

**3. Set this repo's CI variables.** `AI_SERVICE_AUTH_TOKEN`, the same value
as the Backend's. Leave `LLM_SERVER_URL` at its default while Agent-Server is
on this machine. Every other `LLM_*` default is already Agent-Server's
contract.

**4. Check the connection yourself after deploying.** The pipeline's health
check passes on HTTP 200, and `/health` answers 200 even when Agent-Server is
unreachable — it says so in the body. A deploy pointing at the wrong URL goes
green. Ask for the body:

```bash
docker exec soc-ai python -c "import urllib.request, json; h = json.load(urllib.request.urlopen('http://localhost:8100/health')); print(h['llm_server'], h['llm_server_url'], h['upstream'])"
# True http://host.docker.internal:8000 {'status': 'ok'}
```

Then send one call down the whole path (the token is already in the
container's environment). It writes one row to Agent-Server's history, and that
history cannot be deleted, so the prompt says what it was:

```bash
docker exec soc-ai python -c "
import urllib.request, json, os
req = urllib.request.Request('http://localhost:8100/analyze', method='POST',
    data=json.dumps({'rendered_prompt': 'connection test after deploy', 'agent': 'soc-suggest'}).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['AI_SERVICE_AUTH_TOKEN']})
d = json.load(urllib.request.urlopen(req, timeout=300)); print(d['status'], d['error'])"
# completed None
```

Agent-Server logs the same call, or the reason it refused it:

```bash
docker logs dall-m-server 2>&1 | grep dall_m.invoke | tail -3
# ... agent=soc-suggest v1 model=... stream=True in_chars=28 -> success 812ms tokens in=... out=...
# ... agent=soc-suggest version=active rejected: Agent is inactive
```

**5. Close Agent-Server's ports.** Its compose publishes 8000 (API), 8080
(llama.cpp), 7100 (admin UI) and 5497 (Postgres) on every interface.
`/v1/agents/...` takes no token and llama.cpp's own API takes none either, so
anything that can reach the machine can run any agent, or the model directly.
Allow 8000 only from this machine's Docker networks (172.16.0.0/12 by default —
that is where calls from `soc-ai` arrive), 7100 from the analysts' network,
and 8080 and 5497 from nothing.
Do it in front of the machine (security group, network firewall) or in the
`DOCKER-USER` iptables chain: ports Docker publishes bypass ufw's rules. The
day Agent-Server checks a token, set `LLM_AUTH_TOKEN` here.

**Agent-Server on another machine.** Point its `deploy_production` job at that
machine's runner (the `soc-deploy` tag in its `.gitlab-ci.yml`) and set
`LLM_SERVER_URL=http://<that-host>:8000` here. Alerts and answers now cross
the network unencrypted to an API that takes no token, so put a TLS reverse
proxy in front of port 8000 that only accepts this machine, and use its
`https://` address. `LLM_EXTRA_HEADERS` carries a header the proxy checks, if
it wants one.

## Configuration

See `.env.example`; each setting is explained there. The ones that matter:

| Variable | Default | Notes |
| --- | --- | --- |
| `AI_SERVICE_AUTH_TOKEN` | — | Required. Same value as `Backend/.env`. |
| `LLM_SERVER_URL` | `http://host.docker.internal:8000` | Agent-Server, not on `soc-network` — see above. |
| `LLM_RUN_PATH` | `/v1/agents/{agent}/chat` | Agent-Server's invoke route. |
| `LLM_INPUT_FIELD` | `message` | Agent-Server's `AgentChatRequest.message`. |
| `LLM_HEALTH_PATH` | `/` | Agent-Server has no `/health`. |
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

# Tests: no model and no agent platform needed.
pip install -r requirements.txt -r tests/requirements.txt
pytest -q
```

## Running the whole stack

This repo runs on its own with the compose file above, which is enough to work
on it. Running the SOC end to end takes four repos, checked out side by side,
and one script that writes the `.env` files — plus Agent-Server, run
separately (it is not one of the four; see "Agent-Server" above):

```bash
git clone https://gitlab.com/agentic-soc/backend.git      Backend
git clone https://gitlab.com/agentic-soc/frontend.git     Frontend
git clone https://gitlab.com/agentic-soc/ai-service.git   AI-Service
git clone https://gitlab.com/agentic-soc/mcp-server.git   MCP-Server
git clone https://gitlab.com/agentic-soc/Agent-Server.git Agent-Server

cd Backend
./deploy/dev-setup.sh                            # writes .env in Backend, AI-Service, MCP-Server
docker compose -f deploy/dev.yml --project-directory . up -d --build   # http://localhost:3001

cd ../Agent-Server
cp .env.example .env                             # its defaults work out of the box
docker compose up -d --build                     # http://localhost:7100
```

The script lives in Backend because three of the secrets it generates have to
be identical in more than one repo — the tokens the connectors check — and
they are the ones that break quietly: a mismatch is a 401 with nothing saying
why. Generating them in one place is the whole point. It does not touch
Agent-Server: that repo manages its own `.env` and its defaults need no edits
for local development.

---

## Where this came from

Split out of the SOC-AI monorepo. The other pieces:

| Repo | What it is |
| --- | --- |
| `Backend` | API, queue worker, Postgres, Redis. The only writer to the database. |
| `AI-Service` | The backend's door to the agent platform, and the playbook index. |
| `MCP-Server` | Connectors for Splunk, ThreatQ and Confluence. |
| `Frontend` | The analyst dashboard. |

Plus [`Agent-Server`](../Agent-Server/readme.md) (DALL-M): agents as data —
model, prompt, tools — and the loop that runs them. It is a separate system on
its own network, not one of the four above; see "Agent-Server" above for how
this repo reaches it.

They meet on one Docker network called `soc-network`, created by whichever
stack comes up first, and address each other by container name. Bring up
`MCP-Server` first: it is the one the rest call. Agent-Server is not on
`soc-network` and brings itself up on its own.

Each repo carries only the settings its own services read. In the monorepo one
`.env` was shared by every container, which meant the Splunk connector's
environment also held the database password and the ThreatQ credentials — a
compromise of any one service read the secrets of all of them.
