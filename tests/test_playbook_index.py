"""Indexing the pages the backend sends, and matching alerts against them."""

import copy

import pytest

from conftest import AUTH, CORPUS, DNS, FAILED_LOGINS, PHISHING, FakeEmbedder, make_store, match, page
from playbooks import store as store_module


# ── Matching, tier by tier ──────────────────────────────────────────────────

def test_nothing_indexed_is_not_the_same_as_no_playbook(client):
    result = match(client, "Excessive Failed Logins")
    assert result["how"] == "unindexed"
    assert result["matched"] is False
    assert "not the same as no playbook" in result["note"]


def test_exact_name(indexed):
    result = match(indexed, "excessive  failed logins")
    assert (result["how"], result["playbook"]["page_id"]) == ("exact", "101")


def test_alias(indexed):
    result = match(indexed, "T1110")
    assert (result["how"], result["matched_name"]) == ("exact", "T1110")


def test_a_renamed_detection_matches_by_spelling(indexed):
    result = match(indexed, "Excessive Failed Login")
    assert result["how"] == "fuzzy"
    assert result["playbook"]["page_id"] == "101"


def test_different_words_for_the_same_thing_match_by_meaning(indexed):
    result = match(indexed, "many failed login attempts against user accounts")
    assert result["how"] == "semantic"
    assert result["playbook"]["page_id"] == "101"
    assert result["matched_field"] == "rule_description"
    assert 0.5 <= result["similarity"] <= 1.0


def test_nothing_close_is_a_miss_that_says_how_close(indexed):
    result = match(indexed, "Printer out of toner")
    assert result["matched"] is False
    assert result["how"] == "miss"
    assert "Nearest by meaning" in result["note"]


def test_names_are_not_embedded(indexed, embedder):
    # Templated names ("Excessive ...") pull unrelated alerts together; tier 3
    # compares against what a playbook says, not what it is called.
    assert "Excessive Failed Logins" not in embedder.documents
    assert "Many failed login attempts from one source against user accounts" in embedder.documents


def test_close_to_two_playbooks_at_once_is_not_a_match(client):
    twins = [
        page("301", [("Alert Name", "Alpha"), ("Rule Description", "outbound traffic to a rare port")]),
        page("302", [("Alert Name", "Beta"), ("Rule Description", "outbound traffic to a rare host")]),
    ]
    client.put("/playbooks/index", json={"pages": twins}, headers=AUTH)
    result = match(client, "outbound traffic to a rare destination")
    assert result["matched"] is False
    assert "too close together" in result["note"]


def test_the_answer_says_which_corpus_it_came_from(indexed):
    corpus = match(indexed, "Excessive Failed Logins")["corpus"]
    assert corpus["playbooks"] == 3
    assert corpus["source"] == "confluence"
    assert corpus["built_at"]


def test_a_query_is_embedded_once_per_alert_name(indexed, embedder):
    for _ in range(3):
        match(indexed, "Printer out of toner")
    match(indexed, "printer  OUT of toner")  # the same name, normalised
    assert len(embedder.queries) == 1


# ── Building: only what changed is embedded ─────────────────────────────────

def test_the_first_index_embeds_every_field(client, embedder):
    body = client.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH).json()
    # description and criteria, not names: 3 + 2 + 1 across the three pages
    assert (body["embedded"], body["reused"]) == (6, 0)
    assert body["playbooks"] == 3
    assert body["semantic"]["embedded_fields"] == 6


def test_an_unchanged_sync_embeds_nothing(indexed, embedder):
    before = len(embedder.documents)
    body = indexed.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH).json()
    assert (body["embedded"], body["reused"]) == (0, 6)
    assert len(embedder.documents) == before


def test_an_edited_page_costs_only_its_changed_field(indexed, embedder):
    edited = copy.deepcopy(PHISHING)
    edited["body"] = edited["body"].replace("registered recently", "registered this week")
    body = indexed.put("/playbooks/index", json={"pages": [FAILED_LOGINS, edited, DNS]},
                       headers=AUTH).json()
    assert (body["embedded"], body["reused"]) == (1, 5)
    assert embedder.documents[-1] == "The sender domain was registered this week"


def test_a_removed_page_stops_matching(indexed):
    indexed.put("/playbooks/index", json={"pages": [FAILED_LOGINS, DNS]}, headers=AUTH)
    assert match(indexed, "User Reported Phishing")["matched"] is False


def test_a_page_that_is_not_a_playbook_is_reported(client):
    draft = page("999", [("Rule Description", "unfinished")], title="Draft playbook")
    body = client.put("/playbooks/index", json={"pages": CORPUS + [draft]}, headers=AUTH).json()
    assert body["pages"] == 4
    assert body["playbooks"] == 3
    assert body["skipped"][0]["title"] == "Draft playbook"


def test_two_pages_claiming_one_name_are_reported(client):
    twin = page("104", [("Alert Name", "Excessive Failed Logins")])
    body = client.put("/playbooks/index", json={"pages": CORPUS + [twin]}, headers=AUTH).json()
    assert body["conflicts"] == [{"name": "Excessive Failed Logins", "pages": ["101", "104"]}]


# ── Refusals ────────────────────────────────────────────────────────────────

def test_an_empty_sync_does_not_wipe_a_good_index(indexed):
    resp = indexed.put("/playbooks/index", json={"pages": []}, headers=AUTH)
    assert resp.status_code == 409
    assert "allow_empty" in resp.json()["detail"]
    assert match(indexed, "Excessive Failed Logins")["matched"] is True


def test_an_empty_space_can_be_indexed_on_purpose(indexed):
    resp = indexed.put("/playbooks/index", json={"pages": [], "allow_empty": True}, headers=AUTH)
    assert resp.status_code == 200
    assert match(indexed, "Excessive Failed Logins")["how"] == "miss"


@pytest.mark.parametrize("body", [
    {},
    {"pages": "not a list"},
    {"pages": [{"title": "no id"}]},
], ids=["no-pages", "pages-not-a-list", "page-without-id"])
def test_a_malformed_index_request_is_a_422(client, body):
    assert client.put("/playbooks/index", json=body, headers=AUTH).status_code == 422


def test_too_many_pages_is_refused_not_half_read(client):
    many = [page(str(i), [("Alert Name", f"A{i}")]) for i in range(5001)]
    assert client.put("/playbooks/index", json={"pages": many}, headers=AUTH).status_code == 422


@pytest.mark.parametrize("body", [{}, {"alert_name": ""}, {"alert_name": "x" * 513}])
def test_a_malformed_match_request_is_a_422(client, body):
    assert client.post("/playbooks/match", json=body, headers=AUTH).status_code == 422


def test_an_integer_page_id_is_accepted(client):
    p = copy.deepcopy(DNS)
    p["id"] = 103
    assert client.put("/playbooks/index", json={"pages": [p]}, headers=AUTH).status_code == 200
    assert match(client, "Excessive DNS Failures")["playbook"]["page_id"] == "103"


# ── Surviving restarts and failures ─────────────────────────────────────────

def test_a_restart_loads_the_index_without_embedding_again(indexed, store):
    fresh_embedder = FakeEmbedder()
    restarted = make_store(store.path, fresh_embedder, store.vectordb)
    restarted.load()
    store_module.set_store(restarted)

    assert match(indexed, "Excessive Failed Logins")["how"] == "exact"
    assert match(indexed, "many failed login attempts against user accounts")["how"] == "semantic"
    assert fresh_embedder.documents == []  # only the query was embedded

    body = indexed.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH).json()
    assert body["embedded"] == 0


def test_a_different_model_re_embeds_everything(indexed, store):
    other = FakeEmbedder()
    other.name = "another-model"
    restarted = make_store(store.path, other, store.vectordb)
    restarted.load()
    store_module.set_store(restarted)
    body = indexed.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH).json()
    assert (body["embedded"], body["reused"]) == (6, 0)


def test_an_unreadable_index_file_starts_empty(tmp_path):
    path = tmp_path / "index.json"
    path.write_bytes(b"not json")
    s = make_store(path, FakeEmbedder())
    s.load()
    assert s.status()["indexed"] is False


def test_the_write_is_atomic(indexed, store):
    # The temporary file is renamed over the index, never left beside it.
    assert store.path.exists()
    assert not store.path.with_name(store.path.name + ".tmp").exists()


def test_without_a_model_names_still_match(tmp_path, client):
    broken = FakeEmbedder(fail=True)
    s = make_store(tmp_path / "broken.json", broken)
    store_module.set_store(s)

    body = client.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH).json()
    assert body["playbooks"] == 3
    assert body["semantic"]["error"] == "model files not found"

    assert match(client, "Excessive Failed Logins")["how"] == "exact"
    result = match(client, "many failed login attempts against user accounts")
    assert result["matched"] is False
    assert "Matching by meaning was not tried: model files not found" in result["note"]


def test_semantic_off_loads_no_model(tmp_path, client):
    embedder = FakeEmbedder()
    s = make_store(tmp_path / "off.json", embedder, semantic=False)
    store_module.set_store(s)
    client.put("/playbooks/index", json={"pages": CORPUS}, headers=AUTH)
    result = match(client, "many failed login attempts against user accounts")
    assert result["matched"] is False
    assert "PLAYBOOK_SEMANTIC is off" in result["note"]
    assert embedder.documents == [] and embedder.queries == []


# ── Matching by use case id ─────────────────────────────────────────────────

WITH_ID = page("501", [("Alert Name", "Threat - Suspicious IP Callback"), ("Use Case ID", "<p>UC-CAT-012</p>")])


def match_id(client, name, use_case_id=None):
    body = {"alert_name": name} if use_case_id is None else {"alert_name": name, "use_case_id": use_case_id}
    resp = client.post("/playbooks/match", json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def with_id(client):
    assert client.put("/playbooks/index", json={"pages": CORPUS + [WITH_ID]}, headers=AUTH).status_code == 200
    return client


def test_the_id_the_soar_sent_wins_over_a_different_name(with_id):
    result = match_id(with_id, "Some Renamed Detection", "uc-cat-012")
    assert (result["how"], result["playbook"]["page_id"], result["use_case_id"]) == ("id", "501", "UC-CAT-012")


def test_an_id_in_the_alert_name_is_used_when_none_is_sent(with_id):
    result = match_id(with_id, "Threat - UC-CAT-012 - Callback from Client Detected by ThreatQ - Rule")
    assert (result["how"], result["playbook"]["page_id"]) == ("id", "501")


def test_an_id_no_playbook_carries_falls_back_to_the_name(with_id):
    result = match_id(with_id, "Excessive Failed Logins", "UC-NOPE-999")
    assert (result["how"], result["playbook"]["page_id"]) == ("exact", "101")


def test_two_pages_claiming_one_id_are_reported(client):
    twin = page("502", [("Alert Name", "Another"), ("Use Case ID", "UC-CAT-012")])
    body = client.put("/playbooks/index", json={"pages": [WITH_ID, twin]}, headers=AUTH).json()
    assert body["conflicts"] == [{"use_case_id": "UC-CAT-012", "pages": ["501", "502"]}]


@pytest.mark.parametrize("bad", ["UC 012", "x" * 65, "-UC"])
def test_a_malformed_use_case_id_is_a_422(client, bad):
    resp = client.post("/playbooks/match", json={"alert_name": "x", "use_case_id": bad}, headers=AUTH)
    assert resp.status_code == 422
