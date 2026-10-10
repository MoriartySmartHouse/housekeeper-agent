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
    monkeypatch.setattr(agent, "cycle", lambda opts, token: (None, None, None))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    monkeypatch.setattr(agent.time, "sleep", lambda s: handlers[signal.SIGTERM](signal.SIGTERM, None))
    with pytest.raises(SystemExit) as exc:
        agent.main()
    assert exc.value.code == 0


# ---- Watson's sensor comes back after an HA restart (states set over the API are not kept by HA)

LAST = {"state": "all_good", "attributes": {"friendly_name": "Watson", "messages": [], "summary": "Watson: all good"}}


def _fake_ha(monkeypatch, get):
    """Stand-in for agent.http: GETs answer via `get()`, writes are recorded."""
    posts = []

    def http(method, url, token=None, body=None, timeout=30):
        if method == "GET":
            return get()
        posts.append((method, url, body))
    monkeypatch.setattr(agent, "http", http)
    return posts


def _http_error(code):
    return agent.urllib.error.HTTPError("http://supervisor", code, "x", {}, None)


def test_status_missing_after_restart_is_reposted_once_unchanged(monkeypatch):
    def get():
        raise _http_error(404)
    posts = _fake_ha(monkeypatch, get)
    assert agent.repost_status("t", LAST) is True
    assert posts == [("POST", f"{agent.SUPERVISOR}/core/api/states/{agent.STATUS_ENTITY}", LAST)]


def test_status_unknown_is_reposted(monkeypatch):
    posts = _fake_ha(monkeypatch, lambda: {"entity_id": agent.STATUS_ENTITY, "state": "unknown"})
    assert agent.repost_status("t", LAST) is True and len(posts) == 1


def test_status_present_is_left_alone(monkeypatch):
    posts = _fake_ha(monkeypatch, lambda: {"entity_id": agent.STATUS_ENTITY, "state": "attention"})
    assert agent.repost_status("t", LAST) is False and posts == []


def test_ha_down_no_post_no_exception(monkeypatch):
    for err in (_http_error(502), ConnectionRefusedError("down"), TimeoutError("slow")):
        def get(err=err):
            raise err
        posts = _fake_ha(monkeypatch, get)
        assert agent.repost_status("t", LAST) is False and posts == []


def test_no_status_yet_does_nothing(monkeypatch):
    def get():
        raise AssertionError("must not even look before the first run has written a status")
    posts = _fake_ha(monkeypatch, get)
    assert agent.repost_status("t", None) is False and posts == []


def test_repost_write_failure_does_not_raise(monkeypatch):
    def http(method, url, token=None, body=None, timeout=30):
        if method == "GET":
            raise _http_error(404)
        raise ConnectionResetError("HA went away again")
    monkeypatch.setattr(agent, "http", http)
    assert agent.repost_status("t", LAST) is False


def test_wait_glances_between_runs_then_returns(monkeypatch):
    """A 60-min interval: the sensor is looked at every few minutes until the next run is due."""
    clock = [0.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agent.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    seen = []
    monkeypatch.setattr(agent, "repost_status", lambda token, body: seen.append(body))
    agent.wait_for_next_run("t", LAST, 0.0, 3600)
    assert clock[0] == 3600 and len(seen) == 3600 // agent.STATUS_WATCH_S - 1 and seen[0] is LAST


def test_cycle_keeps_the_status_it_wrote(monkeypatch):
    monkeypatch.setattr(agent, "gather", lambda token: ({"errors": {}, "states": None}, "2026.10.0"))
    monkeypatch.setattr(agent, "load_memory", lambda: {})
    posts = _fake_ha(monkeypatch, lambda: None)
    _, _, status = agent.cycle({"site_id": "x", "central_url": "https://c.example", "site_key": "k",
                                "watch": []}, "t")
    assert status["attributes"]["friendly_name"] == "Watson"
    assert posts[-1] == ("POST", f"{agent.SUPERVISOR}/core/api/states/{agent.STATUS_ENTITY}", status)
