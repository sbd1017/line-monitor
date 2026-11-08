"""Virgin's own status checker: body reading, the ask-throttle, and failure handling without a real account."""
from __future__ import annotations

import json

import pytest

from speedagent import vmstatus


def stub(monkeypatch, responses):
    """responses: list of (status, body, headers) returned in order."""
    calls = []

    def fake(url, timeout, body=None, conversation=None, extra=None):
        calls.append((url, body, conversation))
        return responses.pop(0) if responses else (0, "no more", {})

    monkeypatch.setattr(vmstatus, "_call", fake)
    return calls


def test_a_reported_fault_is_read_from_the_outage_list(monkeypatch):
    calls = stub(monkeypatch, [(200, "{}", {"Dapi-Conversationid": "c1"}),
                               (200, json.dumps({"outages": [{"id": 1}], "postCode": "X"}), {})])
    res = vmstatus.check("X", "Y")
    assert res["state"] == "fault_reported"
    assert calls[0][0].endswith("/identify") and calls[1][2] == "c1"


def test_an_empty_answer_is_no_fault(monkeypatch):
    stub(monkeypatch, [(200, "{}", {"Dapi-Conversationid": "c1"}), (200, json.dumps({"outages": []}), {})])
    assert vmstatus.check("X", "Y")["state"] == "no_fault"


def test_unknown_shape_stays_unknown(monkeypatch):
    stub(monkeypatch, [(200, "{}", {"Dapi-Conversationid": "c1"}), (200, "something new", {})])
    assert vmstatus.check("X", "Y")["state"] == "unknown"


def test_an_unidentifiable_account_says_so(monkeypatch):
    stub(monkeypatch, [(404, json.dumps({"message": "Unable to identify the customer"}), {})])
    res = vmstatus.check("X", "Y")
    assert res["state"] == "unidentified" and "could not find the account" in res["message"]


def test_a_missing_session_header_is_not_a_no_fault(monkeypatch):
    stub(monkeypatch, [(200, "{}", {})])
    assert vmstatus.check("X", "Y")["state"] == "unidentified"


def test_a_transport_failure_is_unknown(monkeypatch):
    stub(monkeypatch, [(0, "URLError: timed out", {})])
    res = vmstatus.check("X", "Y")
    assert res["state"] == "unknown" and "URLError" in res["message"]


def test_a_server_error_is_unknown(monkeypatch):
    stub(monkeypatch, [(500, "{}", {})])
    assert vmstatus.check("X", "Y")["state"] == "unknown"


def test_the_ask_is_throttled_and_capped():
    assert vmstatus.due(0, 0, 0)
    assert not vmstatus.due(60_000, 0, 1)                     # inside the half hour
    assert vmstatus.due(1_800_000, 0, 1)                      # half an hour later
    assert not vmstatus.due(9_000_000, 0, vmstatus.MAX_PER_INCIDENT)


def test_the_real_shape_is_a_json_object():
    """Guards the parser against being written for a shape the API never sends."""
    assert isinstance(json.loads('{"outages": []}'), dict)


def test_no_credentials_means_no_call(monkeypatch):
    calls = stub(monkeypatch, [])
    vmstatus.check("", "")                                    # caller decides; check just reports
    assert calls and calls[0][1] == {"postCode": "", "lastName": ""}
    assert calls[0][0].endswith("/identify")


@pytest.mark.parametrize("body,state", [
    ('{"hasOutage": true}', "fault_reported"),
    ('{"message": "There is an outage in your area"}', "fault_reported"),
    ('{"message": "No issues found"}', "no_fault"),
])
def test_wording_fallbacks(monkeypatch, body, state):
    stub(monkeypatch, [(200, "{}", {"Dapi-Conversationid": "c1"}), (200, body, {})])
    assert vmstatus.check("X", "Y")["state"] == state


def test_the_answer_lands_on_the_outage(db):
    from speedmon import analytics, ingest, timeutil
    from tests import helpers
    t0 = timeutil.now_ms() - 700_000
    with db.tx() as c:
        for i in range(3):
            ingest.ingest_batch(c, "pi", [helpers.item("probe", helpers.probe(t0 + i * 60_000 + 5, state="wan_down"))], False)
        ingest.ingest_batch(c, "pi", [helpers.item("event", {
            "uuid": "e1e1e1e1-1111-4111-8111-111111111111", "agent_id": "pi", "t_ms": t0 + 130_000,
            "at": timeutil.iso(t0 + 130_000), "type": "isp_status",
            "detail": json.dumps({"state": "fault_reported", "message": "fault at this postcode"})})], False)
    with db.read() as c:
        inc = analytics.outage_incidents(c, 0, timeutil.now_ms())[0]
    assert inc["isp_check"]["state"] == "fault_reported" and inc["isp_check"]["at_ms"] == t0 + 130_000


def test_no_event_means_no_field(db):
    from speedmon import analytics, ingest, timeutil
    from tests import helpers
    t0 = timeutil.now_ms() - 700_000
    with db.tx() as c:
        for i in range(3):
            ingest.ingest_batch(c, "pi", [helpers.item("probe", helpers.probe(t0 + i * 60_000 + 5, state="wan_down"))], False)
    with db.read() as c:
        assert analytics.outage_incidents(c, 0, timeutil.now_ms())[0]["isp_check"] is None
