import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import agent
import checks

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)


def test_one_broken_check_does_not_kill_the_card(monkeypatch):
    """HIGH-2: a check that raises becomes UNKNOWN; the rest of the card still goes out."""
    def boom(*a, **k):
        raise TypeError("surprise")
    monkeypatch.setattr(checks, "check_batteries", boom)
    out = agent.run_checks({"errors": {}, "states": []}, {"watch": []}, NOW)
    by_id = {c["id"]: c["status"] for c in out}
    assert by_id["devices.battery"] == "UNKNOWN" and len(out) == 9


def test_bad_watch_entries_become_unknown_not_crash():
    rules = agent.parse_watch(['["sensor.x"]', "not json", '{"entity": "sensor.ok", "bad": ["Fault"]}'])
    out = agent.run_checks({"errors": {}, "states": []}, {"watch": rules}, NOW)
    ids = [c["id"] for c in out if c["id"].startswith("watch")]
    assert ids.count("watch.invalid") == 2 and "watch.sensor.ok" in ids


def test_central_empty_household_list_is_respected():
    """MEDIUM-6: [] from central means 'all silenced/fine', not 'no answer'."""
    local = [{"household": "battery low", "status": "WARN"}]
    assert agent.household_list({"household": []}, local) == []
    assert agent.household_list(None, local) == ["battery low"]


def test_central_url_rules():
    assert agent.central_url_allowed("https://hk.example.ca")
    assert agent.central_url_allowed("http://192.168.0.57:8080")
    assert agent.central_url_allowed("http://100.100.1.2:8080")
    assert not agent.central_url_allowed("http://hk.example.ca")
    assert not agent.central_url_allowed("ftp://192.168.0.57")


def test_load_options_rejects_short_key_and_public_http(tmp_path):
    import pytest
    p = tmp_path / "o.json"
    p.write_text(json.dumps({"site_id": "x", "site_key": "short", "central_url": "https://a.ca"}))
    with pytest.raises(SystemExit):
        agent.load_options(p)
    p.write_text(json.dumps({"site_id": "x", "site_key": "k" * 40, "central_url": "http://a.ca"}))
    with pytest.raises(SystemExit):
        agent.load_options(p)
