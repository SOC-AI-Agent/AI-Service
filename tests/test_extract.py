"""Reading a playbook out of a Confluence page's storage XHTML."""

from playbooks.extract import cell_text, from_pages, rows_from_storage
from playbooks.router import AlertRouter

from conftest import FAILED_LOGINS, page


def one(p):
    playbooks, skipped = from_pages([p])
    assert not skipped, skipped
    return playbooks[0]


def test_a_name_wrapped_in_a_paragraph_still_matches_exactly():
    # The connector kept the cell's XHTML, so this name normalised to
    # "p excessive failed logins p" and only ever matched fuzzily.
    playbook = one(FAILED_LOGINS)
    assert playbook.alert_name == "Excessive Failed Logins"
    assert AlertRouter([playbook]).match("Excessive Failed Logins")["how"] == "exact"


def test_criteria_reach_the_model_as_text_not_markup():
    playbook = one(FAILED_LOGINS)
    assert playbook.true_condition == "Source is external & attempts exceed 20 in 5 minutes"
    assert "<" not in playbook.rule_description


def test_aliases_on_separate_lines_stay_separate():
    assert one(FAILED_LOGINS).aliases == ["Brute Force Detected", "T1110"]


def test_nbsp_and_entities_become_plain_characters():
    assert cell_text("<p>A&nbsp;&lt;b&gt;&amp;c</p>") == "A <b>&c"


def test_a_search_in_a_code_macro_keeps_its_characters():
    query = '{"index": "main", "filters": {"action": "blocked"}, "note": "a<b"}'
    p = page("201", [
        ("Alert Name", "<p>Blocked Egress</p>"),
        ("Splunk Query", '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
                         f"<![CDATA[{query}]]></ac:plain-text-body></ac:structured-macro>"),
    ])
    assert one(p).splunk_query == {"index": "main", "filters": {"action": "blocked"}, "note": "a<b"}


def test_a_search_written_as_json_in_a_paragraph_is_read():
    p = page("202", [
        ("Alert Name", "<p>Blocked Egress</p>"),
        ("Splunk Query", '<p>{&quot;index&quot;: &quot;main&quot;}</p>'),
    ])
    assert one(p).splunk_query == {"index": "main"}


def test_a_search_that_is_not_json_is_dropped_not_guessed_at():
    p = page("203", [("Alert Name", "<p>X</p>"), ("Splunk Query", "<p>index=main | stats count</p>")])
    assert one(p).splunk_query is None


def test_rows_that_are_not_properties_are_ignored():
    xhtml = ("<table><tr><th>Heading only</th></tr>"
             "<tr><td>a</td><td>b</td><td>c</td></tr>"
             "<tr><th>Alert Name</th><td>Real</td></tr></table>")
    assert rows_from_storage(xhtml) == {"Alert Name": "Real"}


def test_an_unknown_label_is_ignored_rather_than_guessed():
    p = page("204", [("Alert Name", "Y"), ("Owner's favourite colour", "blue")])
    assert "blue" not in str(one(p).as_dict())


def test_a_page_with_no_alert_name_is_skipped_and_said_so():
    playbooks, skipped = from_pages([page("205", [("Rule Description", "no name here")], title="Draft")])
    assert playbooks == []
    assert [(s["page_id"], s["title"]) for s in skipped] == [("205", "Draft")]
    assert skipped[0]["reason"].startswith("no alert name")


# ── The SOC's layout: headed sections, the title as the name, a version history ──

def soc_page(page_id="401", title="Endpoint - Suspicious Service Installed", sections=None, history=None):
    """A playbook as the SOC writes it: a version-history table, then a heading per field."""
    history = history if history is not None else [("1.0", "2026-11-01", "first"), ("1.2", "2026-11-10", "x"),
                                                   ("1.10", "2026-11-17", "newest by number, not by row")]
    rows = "".join(f"<tr><td><p>{v}</p></td><td><p>{d}</p></td><td><p>{c}</p></td></tr>" for v, d, c in history)
    table = (f"<table><tbody><tr><th><p>Version</p></th><th><p>Date</p></th><th><p>Changes</p></th></tr>{rows}"
             "</tbody></table>") if history else ""
    sections = sections if sections is not None else [
        ("h2", "Use Case ID", "<p>UC-EDR-001</p>"),
        ("h2", "Rule Description", "<p>A new service with a random name</p>"),
        ("h1", "Workaround", ""),
        ("h2", "True Condition:", "<ul><li><p>Event ID 7045</p></li><li><p>Runs as LocalSystem</p></li></ul>"),
        ("h2", "False Condition:", "<ul><li><p>An approved change</p></li></ul>"),
        ("h2", "Steps to Work (For Tier 1/Shift lead):", "<ol><li><p>Verify Host</p></li></ol>"),
        ("h1", "INCIDENT TEMPLATE", "<p><strong>##Identification##</strong></p>"),
    ]
    body = table + "".join(f"<{h}>{label}</{h}>{content}" for h, label, content in sections)
    return {"id": page_id, "title": title, "version": 1, "url": "", "body": body}


def test_headed_sections_are_read_and_the_title_is_the_name():
    playbook = one(soc_page())
    assert playbook.alert_name == "Endpoint - Suspicious Service Installed"
    assert playbook.use_case_id == "UC-EDR-001"
    assert playbook.true_condition == "Event ID 7045\nRuns as LocalSystem"
    assert playbook.false_condition == "An approved change"
    # The label's colon and its "(For Tier 1/Shift lead)" do not stop it being read.
    assert playbook.work_around == "Verify Host"


def test_a_heading_with_only_sub_headings_does_not_swallow_them():
    playbook = one(soc_page())
    assert "Event ID 7045" not in playbook.work_around
    assert "Identification" not in playbook.as_dict().values()


def test_the_version_is_the_newest_row_of_the_history_by_number():
    assert one(soc_page()).version == "1.10"


def test_a_two_column_history_header_is_not_read_as_a_version():
    p = soc_page()
    p["body"] = ("<table><tbody><tr><th><p>Version</p></th><th><p>Date</p></th></tr>"
                 "<tr><td><p>2.0</p></td><td><p>2026-11-17</p></td></tr></tbody></table>") + p["body"].split("</table>", 1)[1]
    assert one(p).version == "2.0"


def test_a_properties_table_opening_on_version_is_not_a_history():
    p = page("402", [("Version", "<p>3</p>"), ("Alert Name", "<p>Blocked Egress</p>")])
    assert one(p).version == "3"


def test_a_titled_page_without_playbook_sections_is_not_a_playbook():
    # The space home page has a Description heading and a title, and is not a detection.
    home = soc_page("403", "SOC Playbooks", sections=[("h2", "Description", "<p>What this space is for</p>")], history=[])
    playbooks, skipped = from_pages([home])
    assert playbooks == [] and skipped[0]["title"] == "SOC Playbooks"


def test_a_table_row_outranks_a_heading_of_the_same_label():
    p = soc_page()
    p["body"] += "<table><tbody><tr><th><p>Alert Name</p></th><td><p>From The Table</p></td></tr></tbody></table>"
    assert one(p).alert_name == "From The Table"


def test_the_use_case_id_is_read_from_the_title():
    p = soc_page(title="Endpoint - UC-EDR-001 - Suspicious Service Installed",
                 sections=[("h2", "True Condition", "<p>Event ID 7045</p>")], history=[])
    playbook = one(p)
    assert (playbook.alert_name, playbook.use_case_id) == ("Endpoint - UC-EDR-001 - Suspicious Service Installed", "UC-EDR-001")
    assert AlertRouter([playbook]).match("anything", "uc-edr-001")["how"] == "id"


def test_a_title_with_a_code_but_no_criteria_is_not_a_playbook():
    playbooks, _ = from_pages([soc_page(title="Web - UC-WEB-009 - Draft", sections=[], history=[])])
    assert playbooks == []


def test_a_history_table_under_the_overview_is_not_part_of_the_section_above_it():
    # The SOC layout: overview first, then the version history, then the workaround.
    history = ("<table><tbody><tr><th><p>Version</p></th><th><p>Date</p></th></tr>"
               "<tr><td><p>1.1</p></td><td><p>2026-11-17</p></td></tr></tbody></table>")
    body = ("<h1>Overview</h1><h2>Use Case Category</h2><p>Malicious Code</p>"
            "<h2>Aliases</h2><p>Web Shell Detected, Suspicious Upload</p>" + history +
            "<h1>Workaround</h1><h2>True Condition:</h2><p>Status 200 to a script</p>")
    playbook = one({"id": "601", "title": "Web - UC-WEB-002 - Web Shell Access Detected", "url": "", "body": body})
    assert playbook.aliases == ["Web Shell Detected", "Suspicious Upload"]
    assert (playbook.version, playbook.use_case_category) == ("1.1", "Malicious Code")


def test_a_title_led_by_its_code_still_matches_the_bare_name():
    p = soc_page(title="UC-WEB-001 - Directory Brute Force Detected",
                 sections=[("h2", "True Condition", "<p>many 404s</p>")], history=[])
    router = AlertRouter([one(p)])
    assert router.match("Directory Brute Force Detected")["how"] == "exact"
    assert router.match("UC-WEB-001 - Directory Brute Force Detected")["how"] in ("id", "exact")


def test_a_detection_rule_in_splunk_query_is_documentation_not_a_search():
    # The SOC layout puts the rule's SPL under Splunk Query; the backend then plans its own searches.
    rule = "<pre><code>index=main sourcetype=wineventlog EventCode=7045 | table _time, Service_Name</code></pre>"
    p = soc_page(title="UC-EDR-001 - S", sections=[("h2", "True Condition", "<p>x</p>"), ("h1", "Splunk Query", rule)], history=[])
    assert one(p).splunk_query is None
