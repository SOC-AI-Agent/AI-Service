"""
In-process tests: the app under TestClient, a stand-in embedder, a temporary
index file. Nothing here needs torch, a model download, or the LLM server.
"""

import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "ai-service-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Before main is imported: it refuses to start without a token, and /health
# asks the LLM server, which here is a closed port that refuses at once.
os.environ["AI_SERVICE_AUTH_TOKEN"] = TOKEN
os.environ["LLM_SERVER_URL"] = "http://127.0.0.1:9"
os.environ["PLAYBOOK_INDEX_PATH"] = str(ROOT / "tests" / ".unused-index.npz")
# Set explicitly rather than left to fall through to .env.example's defaults
# (which a developer's own .env may not match, or may not even be present at
# all in CI): test_agent_server_contract.py checks these are Agent-Server's
# real contract, and a real .env sitting in this checkout must not decide
# whether that check means anything.
os.environ["LLM_RUN_PATH"] = "/v1/agents/{agent}/chat"
os.environ["LLM_INPUT_FIELD"] = "message"
os.environ["LLM_HEALTH_PATH"] = "/"

from playbooks import store as store_module  # noqa: E402
from playbooks.embedder import EmbedderUnavailable  # noqa: E402

DIM = 256
_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """
    Bag of words, hashed into 256 dimensions. Texts sharing words land near
    each other, which is all the matching logic needs to be exercised.

    It also records what it was asked and where: the tests check a rebuild
    embeds only what changed, and that no encode ran on the event loop.
    """

    name = "fake-bow-256"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.on_event_loop = 0

    def _note_thread(self) -> None:
        try:
            asyncio.get_running_loop()
            self.on_event_loop += 1
        except RuntimeError:
            pass  # a worker thread: where blocking work belongs

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, np.float32)
        for word in _WORD.findall(text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def encode_documents(self, texts):
        self._note_thread()
        if self.fail:
            raise EmbedderUnavailable("model files not found")
        self.documents.extend(texts)
        return np.stack([self._vector(t) for t in texts])

    def encode_query(self, text):
        self._note_thread()
        if self.fail:
            raise EmbedderUnavailable("model files not found")
        self.queries.append(text)
        return self._vector(text)

    def warm(self):
        self._note_thread()


def page(page_id, rows, title="", url=""):
    """A Confluence page whose Page Properties table holds these rows."""
    body = "".join(f"<tr><th><p>{label}</p></th><td>{value}</td></tr>" for label, value in rows)
    return {
        "id": page_id, "title": title or page_id, "version": 1, "url": url,
        "body": f'<ac:structured-macro ac:name="details"><ac:rich-text-body><table><tbody>'
                f"{body}</tbody></table></ac:rich-text-body></ac:structured-macro>",
    }


FAILED_LOGINS = page("101", [
    ("Use Case / Alert Name", "<p>Excessive Failed Logins</p>"),
    ("Aliases", "<p>Brute Force Detected<br/>T1110</p>"),
    ("Rule Description", "<p>Many failed login attempts from one source against user accounts</p>"),
    ("True Condition", "<p>Source is external &amp; attempts exceed 20 in 5 minutes</p>"),
    ("False Condition", "<p>Source is the vulnerability scanner</p>"),
])
PHISHING = page("102", [
    ("Alert Name", "<p>User Reported Phishing</p>"),
    ("Rule Description", "<p>An employee forwarded a suspicious email with a link</p>"),
    ("True Condition", "<p>The sender domain was registered recently</p>"),
])
DNS = page("103", [
    ("Alert Name", "<p>Excessive DNS Failures</p>"),
    ("Rule Description", "<p>A host generates many NXDOMAIN responses, typical of DGA malware</p>"),
])
CORPUS = [FAILED_LOGINS, PHISHING, DNS]


def make_store(path, embedder=None, **kwargs):
    kwargs.setdefault("min_score", 0.5)
    return store_module.PlaybookStore(path, embedder, **kwargs)


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def store(tmp_path, embedder):
    s = make_store(tmp_path / "index.npz", embedder)
    store_module.set_store(s)
    yield s
    store_module.set_store(None)
    # Every test, not one: an encode on the event loop stalls /analyze's stream.
    assert embedder.on_event_loop == 0, "an encode ran on the event loop and blocked it"


@pytest.fixture
def client(store):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def indexed(client):
    resp = client.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return client


def match(client, name):
    resp = client.post("/playbooks/match", json={"alert_name": name}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()
