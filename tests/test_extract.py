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
    assert skipped == [{"page_id": "205", "title": "Draft",
                        "reason": "no alert name in its Page Properties table"}]
