import json
import logging

from pydantic_settings import BaseSettings

logger = logging.getLogger("config")


class Settings(BaseSettings):
    APP_NAME: str = "SOC AI Service"
    APP_ENV: str = "development"
    DEBUG: bool = False

    # Agent-Server (DALL-M) — see ../Agent-Server/readme.md. Not on soc-network:
    # it is its own stack (own Postgres, own compose, own dall-m-net), reached
    # the same way Splunk/ThreatQ/Confluence are, by URL rather than container
    # name. host.docker.internal reaches its published port 8000 when it runs
    # on the same machine; point this at wherever it actually runs otherwise.
    LLM_SERVER_URL: str = "http://host.docker.internal:8000"
    # Path to run an agent. `{agent}` is substituted; a server that takes the
    # agent in the body instead can use a static path and LLM_AGENT_FIELD.
    # Agent-Server: POST /v1/agents/{slug}/chat.
    LLM_RUN_PATH: str = "/v1/agents/{agent}/chat"
    # Which agent handles an alert when the request does not name one.
    LLM_DEFAULT_AGENT: str = "soc-suggest"

    # Request body field names, because these are the ones that differ between
    # implementations. Leave LLM_AGENT_FIELD empty if the agent is in the path.
    # Agent-Server's AgentChatRequest reads the alert text as "message".
    LLM_INPUT_FIELD: str = "message"
    LLM_AGENT_FIELD: str = ""
    # Path this service's own health check asks the agent platform for.
    # Agent-Server has no /health — its root answers {"status": "ok"}.
    LLM_HEALTH_PATH: str = "/"
    # Extra body fields as JSON, e.g. {"stream": true, "save": false}
    LLM_EXTRA_BODY: str = ""

    # "auto"   — ask for a stream, accept whatever comes back
    # "always" — treat the body as a stream regardless of content type
    # "never"  — expect one JSON object
    LLM_STREAM_MODE: str = "auto"

    # Bearer token, if their service wants one.
    LLM_AUTH_TOKEN: str = ""
    # Any other headers, as JSON.
    LLM_EXTRA_HEADERS: str = ""

    # An agent run makes several model and tool calls, so this is much longer
    # than one generation needs.
    # Longer than the LLM server's own AGENT_RUN_TIMEOUT so that server reports
    # its own failure rather than this one cutting the connection first and
    # turning a legible error into a timeout.
    LLM_TIMEOUT: int = 1860

    # Bearer token the backend presents on every route except /health. Must
    # match AI_SERVICE_AUTH_TOKEN in Backend/.env. Unset = refuses to start:
    # anything on soc-network could otherwise run agents and rewrite the
    # playbook index.
    AI_SERVICE_AUTH_TOKEN: str = ""

    # ── Playbook index ───────────────────────────────────────────────────────
    # The backend reads the playbook pages out of Confluence (through the
    # connector) and sends them here; this service extracts the fields, embeds
    # them and answers "which playbook covers this alert". The index is one
    # file on a volume, so a restart does not mean embedding everything again.
    PLAYBOOK_INDEX_PATH: str = "/data/playbook-index.npz"
    # Tier 2: a renamed detection still finds its playbook by spelling.
    PLAYBOOK_FUZZY_CUTOFF: float = 0.85
    # Tier 3: matching by meaning when neither the name nor its spelling does.
    # false leaves tiers 1 and 2 only, and no model is loaded.
    PLAYBOOK_SEMANTIC: bool = True
    # A meaning match must reach this cosine similarity AND lead the next
    # playbook by this much. A wrong playbook is worse than none — the analyst
    # reads criteria for another detection — so both err high. Measured with
    # bge-small on the notebook's six sample playbooks and 26 alert names:
    # 0.75/0.05 took 8 of 12 renamed detections and 0 of 14 unrelated ones.
    # Six playbooks is not your wiki: recalibrate on the real pages.
    PLAYBOOK_SEMANTIC_MIN_SCORE: float = 0.75
    PLAYBOOK_SEMANTIC_MIN_MARGIN: float = 0.05
    # More than this in one index request is refused rather than half-read.
    PLAYBOOK_MAX_PAGES: int = 5000

    # The embedding model runs in this process, on CPU. Agent-Server's llama.cpp
    # is not an option: it answers chat completions only, over a narrow HTTP API
    # this service has no other access to, not a shared inference engine.
    EMBED_MODEL: str = "BAAI/bge-small-en-v1.5"
    # BGE models are trained with this prefix on the query side only.
    EMBED_QUERY_PREFIX: str = "Represent this sentence for searching relevant passages: "
    # Where the weights are. The image bakes them in here at build time, and
    # runs with HF_HUB_OFFLINE=1 so nothing is fetched from the internet at runtime.
    EMBED_CACHE_DIR: str = "/models"

    model_config = {"env_file": ".env", "case_sensitive": True, "extra": "ignore"}

    def _json_setting(self, raw: str, name: str) -> dict:
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Not fatal: a typo in an optional override should not stop the
            # service from answering, and the default path still works.
            logger.error("%s is not valid JSON, ignoring it: %s", name, exc)
            return {}
        if not isinstance(parsed, dict):
            logger.error("%s must be a JSON object, ignoring it", name)
            return {}
        return parsed

    @property
    def extra_body(self) -> dict:
        return self._json_setting(self.LLM_EXTRA_BODY, "LLM_EXTRA_BODY")

    @property
    def extra_headers(self) -> dict[str, str]:
        return {k: str(v) for k, v in self._json_setting(
            self.LLM_EXTRA_HEADERS, "LLM_EXTRA_HEADERS").items()}

    def run_url(self, agent: str) -> str:
        path = self.LLM_RUN_PATH.replace("{agent}", agent)
        if not path.startswith("/"):
            path = "/" + path
        return self.LLM_SERVER_URL.rstrip("/") + path


settings = Settings()
