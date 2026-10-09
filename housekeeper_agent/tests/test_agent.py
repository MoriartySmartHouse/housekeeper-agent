import json
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    assert by_id["devices.battery"] == "UNKNOWN" and len(out) == 15   # 0.1.7: + ha.integrations, ha.log_errors


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


def test_watch_entities_form_fields():
    rules = agent.parse_watch_entities([{"entity": "sensor.alarm_status", "bad_states": "Fault, Offline",
                                         "for_minutes": 30}, {"bad_states": "x"}])
    assert rules[0] == {"entity": "sensor.alarm_status", "bad": ["Fault", "Offline"], "for_min": 30}
    assert "invalid" in rules[1]


def test_memory_round_trip_and_corrupt_file(tmp_path):
    p = tmp_path / "m.json"
    agent.save_memory({"silent_since": {"d1": "2026-10-01T00:00:00+00:00"}}, p)
    assert agent.load_memory(p)["silent_since"]["d1"].startswith("2026-10-01")
    p.write_text("{not json")
    assert agent.load_memory(p) == {}


def test_version_matches_config():
    cfg = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
    assert f'version: "{agent.VERSION}"' in cfg


def test_sigterm_stops_cleanly(monkeypatch):
    """As PID 1 the agent must handle SIGTERM itself, or Supervisor SIGKILLs it after 10 s (exit 137)."""
    handlers = {}
    monkeypatch.setattr(agent.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(agent, "load_options", lambda: {"site_id": "test", "interval_minutes": 60})
    monkeypatch.setattr(agent, "cycle", lambda opts, token: None)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    monkeypatch.setattr(agent.time, "sleep", lambda s: handlers[signal.SIGTERM](signal.SIGTERM, None))
    with pytest.raises(SystemExit) as exc:
        agent.main()
    assert exc.value.code == 0
