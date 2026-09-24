"""The bearer gate: every route but /health, and no way around it."""

import os
import subprocess
import sys

import pytest

from conftest import AUTH, ROOT, TOKEN

PROTECTED = [
    ("get", "/playbooks", None),
    ("post", "/playbooks/match", {"alert_name": "x"}),
    ("put", "/playbooks/index", {"pages": []}),
    ("post", "/analyze", {"rendered_prompt": "x"}),
    ("post", "/llm-probe", {"rendered_prompt": "x"}),
    ("get", "/docs", None),
    ("get", "/openapi.json", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED, ids=[p for _, p, _ in PROTECTED])
def test_no_token_is_a_401(client, method, path, body):
    resp = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize(
    "header",
    [f"Bearer {TOKEN}x", f"Basic {TOKEN}", "Bearer", "", f"bearer  {TOKEN}"],
    ids=["wrong-token", "wrong-scheme", "no-token", "empty", "double-space"],
)
def test_a_bad_credential_is_a_401(client, header):
    resp = client.get("/playbooks", headers={"Authorization": header})
    assert resp.status_code == 401


def test_a_non_ascii_token_is_a_401_not_a_crash(client):
    resp = client.get("/playbooks", headers={"Authorization": "Bearer café".encode("latin-1")})
    assert resp.status_code == 401


def test_the_right_token_gets_through(client):
    assert client.get("/playbooks", headers=AUTH).status_code == 200


def test_the_scheme_is_case_insensitive(client):
    assert client.get("/playbooks", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_health_needs_no_token(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["llm_server"] is False  # the closed port conftest points at


def test_it_refuses_to_start_without_a_token():
    env = {k: v for k, v in os.environ.items() if k != "AI_SERVICE_AUTH_TOKEN"}
    env["AI_SERVICE_AUTH_TOKEN"] = ""
    proc = subprocess.run([sys.executable, "-c", "import main"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "AI_SERVICE_AUTH_TOKEN" in proc.stderr
