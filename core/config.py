import json
import logging

from pydantic_settings import BaseSettings

logger = logging.getLogger("config")


class Settings(BaseSettings):
    APP_NAME: str = "SOC AI Service"
    APP_ENV: str = "development"
    DEBUG: bool = False

    LLM_SERVER_URL: str = "http://soc-llm-server:8400"
    # Path to run an agent. `{agent}` is substituted; a server that takes the
    # agent in the body instead can use a static path and LLM_AGENT_FIELD.
    LLM_RUN_PATH: str = "/agents/{agent}/run"
    # Which agent handles an alert when the request does not name one.
    LLM_DEFAULT_AGENT: str = "soc-triage"

    # Request body field names, because these are the ones that differ between
    # implementations. Leave LLM_AGENT_FIELD empty if the agent is in the path.
    LLM_INPUT_FIELD: str = "input"
    LLM_AGENT_FIELD: str = ""
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
