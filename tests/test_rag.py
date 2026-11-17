"""
The retrieval side of the playbook index: runbook steps and detection SPL as
metadata, the vector store, passage search, and the use case guard that keeps
an alert from reaching another detection's playbook.
"""

import pytest

from conftest import AUTH, CORPUS, FakeEmbedder, make_store, page
from playbooks import store as store_module
from playbooks.extract import from_pages, split_steps, spl_scope

STEPS = ("<ol><li>Check the source IP against the scanner list</li>"
         "<li>Search firewall logs for the source IP in the last 24 hours</li>"
         "<li>Escalate to Tier 2 when the account is privileged</li></ol>")
SPL = ('<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA['
       'index=fortigate sourcetype="fgt_traffic" action=blocked | stats count by src_ip'
       ']]></ac:plain-text-body></ac:structured-macro>')

CALLBACK = page("601", [
    ("Alert Name", "<p>Suspicious IP Callback</p>"),
    ("Use Case ID", "<p>UC-CAT-012</p>"),
    ("Rule Description", "<p>A client connects to an address ThreatQ lists as command and control</p>"),
    ("True Condition", "<p>The destination is on the ThreatQ block list</p>"),
    ("False Condition", "<p>The destination is the company proxy</p>"),
    ("Steps to Work", STEPS),
    ("Splunk Query", SPL),
])


def post(client, path, body):
    resp = client.post(path, json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def rag(client):
    resp = client.put("/playbooks/index", json={"pages": CORPUS + [CALLBACK]}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return client


# ── Extraction ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, steps", [
    ("1. Check A\n2) Check B\nStep 3: Check C", ["Check A", "Check B", "Check C"]),
    ("- one\n• two\n\n   \n* three", ["one", "two", "three"]),
    ("10.1.1.1 is the scanner", ["10.1.1.1 is the scanner"]),
    ("", []),
])
def test_split_steps(text, steps):
    assert split_steps(text) == steps


def test_spl_scope_names_places_not_patterns():
    scope = spl_scope('index=fortigate OR index="fw-edge" sourcetype=fgt_* sourcetype::pan:traffic index=fortigate')
    assert scope == {"index": ["fortigate", "fw-edge"], "sourcetype": ["pan:traffic"]}


def test_steps_and_detection_spl_are_metadata():
    [playbook], _ = from_pages([CALLBACK])
    d = playbook.as_dict()
    assert d["steps"] == [
        "Check the source IP against the scanner list",
        "Search firewall logs for the source IP in the last 24 hours",
        "Escalate to Tier 2 when the account is privileged",
    ]
    assert d["detection_spl"].startswith("index=fortigate")
    assert d["detection_scope"] == {"index": ["fortigate"], "sourcetype": ["fgt_traffic"]}
    # SPL is never passed off as the parameters the backend would run.
    assert d["splunk_query"] is None


def test_json_parameters_are_not_mistaken_for_spl():
    p = page("602", [("Alert Name", "X"), ("Splunk Query", '{"index": "main"}')])
    [playbook], _ = from_pages([p])
    assert playbook.splunk_query == {"index": "main"}
    assert playbook.detection_spl == ""


# ── The vector store ────────────────────────────────────────────────────────

def test_steps_are_embedded_one_point_each(rag, embedder):
    status = rag.get("/playbooks", headers=AUTH).json()
    assert status["semantic"]["embedded_steps"] == 3
    assert status["semantic"]["embedded_fields"] == 9
    assert status["vectordb"]["ready"] is True
    assert status["vectordb"]["collection"] == "playbooks__fake-bow-256"
    assert "Escalate to Tier 2 when the account is privileged" in embedder.documents


def test_steps_are_never_matched_on(rag):
    # Word for word a step, and still not a playbook for the alert.
    result = post(rag, "/playbooks/match", {"alert_name": "Search firewall logs for the source IP in the last 24 hours"})
    assert result.get("matched_field") != "step"


def test_a_fresh_qdrant_is_filled_from_the_file(rag, store):
    # Same playbooks on disk, an empty vector store: the first need re-embeds them.
    fresh = FakeEmbedder()
    restarted = make_store(store.path, fresh)
    restarted.load()
    store_module.set_store(restarted)
    result = post(rag, "/playbooks/match", {"alert_name": "many failed login attempts against user accounts"})
    assert result["how"] == "semantic"
    assert len(fresh.documents) == 12


def test_a_removed_page_leaves_no_points(rag, store):
    rag.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH)
    assert store.vectordb.count(store.collection) == 6


# ── Search ──────────────────────────────────────────────────────────────────

def test_search_finds_the_step(rag):
    body = post(rag, "/playbooks/search", {"query": "firewall logs for the source IP", "k": 3})
    top = body["hits"][0]
    assert (top["page_id"], top["field"], top["step"]) == ("601", "step", 2)
    assert top["use_case_id"] == "UC-CAT-012"
    assert top["text"] == "Search firewall logs for the source IP in the last 24 hours"


def test_search_within_one_playbook(rag):
    body = post(rag, "/playbooks/search", {"query": "source", "page_id": "101", "k": 10})
    assert body["hits"] and {h["page_id"] for h in body["hits"]} == {"101"}


def test_search_by_field(rag):
    body = post(rag, "/playbooks/search", {"query": "proxy", "fields": ["false_condition"], "k": 10})
    assert body["hits"] and {h["field"] for h in body["hits"]} == {"false_condition"}


def test_search_before_any_index(client):
    body = post(client, "/playbooks/search", {"query": "anything"})
    assert body["hits"] == [] and "No playbooks" in body["note"]


@pytest.mark.parametrize("body", [{}, {"query": ""}, {"query": "x", "k": 0}, {"query": "x", "k": 21},
                                  {"query": "x", "fields": ["contact"]}])
def test_a_malformed_search_is_a_422(client, body):
    assert client.post("/playbooks/search", json=body, headers=AUTH).status_code == 422


# ── The use case guard ──────────────────────────────────────────────────────

def test_the_use_case_id_still_wins(rag):
    result = post(rag, "/playbooks/match", {"alert_name": "Anything", "use_case_id": "UC-CAT-012"})
    assert (result["how"], result["playbook"]["page_id"]) == ("id", "601")
    assert result["playbook"]["steps"][1].startswith("Search firewall logs")


def test_a_name_of_another_use_case_is_refused(rag):
    result = post(rag, "/playbooks/match", {"alert_name": "Suspicious IP Callback", "use_case_id": "UC-NET-777"})
    assert result["matched"] is False
    assert result["how"] == "use_case_mismatch"
    assert "UC-CAT-012" in result["note"] and "UC-NET-777" in result["note"]


def test_a_misspelt_name_of_another_use_case_is_refused(rag):
    result = post(rag, "/playbooks/match", {"alert_name": "Suspicious IP Callbacks", "use_case_id": "UC-NET-777"})
    assert result["how"] == "use_case_mismatch"


def test_meaning_never_reaches_another_use_case(rag):
    near = "client connects to an address ThreatQ lists as command and control"
    assert post(rag, "/playbooks/match", {"alert_name": near})["playbook"]["page_id"] == "601"
    result = post(rag, "/playbooks/match", {"alert_name": near, "use_case_id": "UC-NET-777"})
    assert result["matched"] is False


def test_a_playbook_without_an_id_is_still_reachable_by_name(rag):
    result = post(rag, "/playbooks/match", {"alert_name": "Excessive Failed Logins", "use_case_id": "UC-NET-777"})
    assert (result["how"], result["playbook"]["page_id"]) == ("exact", "101")
