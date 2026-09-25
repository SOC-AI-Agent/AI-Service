"""
core/config.py's own field defaults against what .env.example documents, and
against each other. These are meant to describe the same thing twice — the
class is what ships if a deploy forgets to mount an env file at all, the
env.example is what a person actually edits — and them drifting apart is a
silent way for a real deploy to end up on `.env.example`'s promise while a
test or an image without one quietly runs on something else.

conftest.py sets several of these in os.environ so the *other* test files are
deterministic regardless of a developer's own .env; this file is the one place
that deliberately steps around that, to look at the class's bare defaults.
"""

import os
from pathlib import Path

from core.config import Settings

ROOT = Path(__file__).resolve().parent.parent
CHECKED = ("LLM_SERVER_URL", "LLM_RUN_PATH", "LLM_INPUT_FIELD", "LLM_HEALTH_PATH")


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _bare_defaults() -> Settings:
    """A Settings() that sees neither this checkout's .env.example nor .env,
    nor conftest's os.environ overrides — only the field defaults in the class."""
    removed = {k: os.environ.pop(k, None) for k in CHECKED}
    try:
        return Settings(_env_file=None)
    finally:
        for key, value in removed.items():
            if value is not None:
                os.environ[key] = value


def test_env_example_matches_the_class_defaults():
    """Whoever edits one of these and not the other finds out here, not in
    production the day the env file mounted is the packaged image's own."""
    documented = _env_example_values()
    defaults = _bare_defaults()
    for key in CHECKED:
        assert documented[key] == getattr(defaults, key), (
            f"{key}: .env.example says {documented[key]!r}, "
            f"core/config.py's default is {getattr(defaults, key)!r}"
        )


def test_the_bare_defaults_point_at_agent_server():
    # Agent-Server (DALL-M): POST /v1/agents/{slug}/chat, {"message": ...},
    # health at / — see ../Agent-Server/server/app/routers/agent_invoke.py,
    # module/agent/schemas.py and main.py.
    defaults = _bare_defaults()
    assert defaults.LLM_RUN_PATH == "/v1/agents/{agent}/chat"
    assert defaults.LLM_INPUT_FIELD == "message"
    assert defaults.LLM_HEALTH_PATH == "/"
    assert defaults.run_url("soc-suggest") == (
        defaults.LLM_SERVER_URL.rstrip("/") + "/v1/agents/soc-suggest/chat"
    )
