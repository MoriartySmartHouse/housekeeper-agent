import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import checks  # noqa: E402

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)


def iso(h_ago):
    return (NOW - timedelta(hours=h_ago)).isoformat()


# ---- fail loudly: unreadable / changed sources are UNKNOWN, never PASS

def test_every_check_is_unknown_on_garbage():
    assert checks.check_backup_offsite(None, NOW)["status"] == "UNKNOWN"
    assert checks.check_backup_offsite({"backups": [{"date": iso(1)}]}, NOW)["status"] == "UNKNOWN"
    assert checks.check_backup_last_attempt({}, NOW)["status"] == "UNKNOWN"
    assert checks.check_repairs(None)["status"] == "UNKNOWN"
    assert checks.check_notifications("x")["status"] == "UNKNOWN"
    assert checks.check_updates(None)["status"] == "UNKNOWN"
    assert checks.check_silent_devices([], [], None, NOW)["status"] == "UNKNOWN"
    assert checks.check_silent_devices([], [], [], NOW)["status"] == "UNKNOWN"  # nothing found = suspicious
    assert checks.check_batteries([], [])["status"] == "UNKNOWN"
    assert checks.check_supervisor({})["status"] == "UNKNOWN"
    assert checks.check_watched_entity([], {"entity": "sensor.x"}, NOW)["status"] == "UNKNOWN"


# ---- backups

def test_offsite_backup_fresh_passes_and_local_only_fails():
    info = {"backups": [{"date": iso(8), "agents": {"hassio.local": {}, "cloud.cloud": {}}}]}
    assert checks.check_backup_offsite(info, NOW)["status"] == "PASS"
    local_only = {"backups": [{"date": iso(1), "agents": {"hassio.local": {}}}]}
    r = checks.check_backup_offsite(local_only, NOW)
    assert r["status"] == "FAIL" and r["audience"] == "household"


def test_offsite_backup_ages_and_old_agent_ids_format():
    assert checks.check_backup_offsite({"backups": [{"date": iso(30), "agent_ids": ["cloud.cloud"]}]}, NOW)["status"] == "WARN"
    assert checks.check_backup_offsite({"backups": [{"date": iso(80), "agent_ids": ["cloud.cloud"]}]}, NOW)["status"] == "FAIL"


def test_last_attempt_failed():
    info = {"last_attempted_automatic_backup": iso(5), "last_completed_automatic_backup": iso(29)}
    assert checks.check_backup_last_attempt(info, NOW)["status"] == "FAIL"
    info = {"last_attempted_automatic_backup": iso(5), "last_completed_automatic_backup": iso(4.9)}
    assert checks.check_backup_last_attempt(info, NOW)["status"] == "PASS"


# ---- repairs / notifications

def test_repairs_ignored_are_not_counted():
    issues = [{"domain": "a", "issue_id": "1", "ignored": True},
              {"domain": "b", "issue_id": "2", "ignored": False, "severity": "warning"}]
    r = checks.check_repairs(issues)
    assert r["status"] == "WARN" and r["evidence"]["count"] == 1
    assert checks.check_repairs([{"domain": "c", "issue_id": "3", "severity": "critical"}])["status"] == "FAIL"


# ---- devices: the ZB-01 case

def _device_fixture(state, h_ago):
    states = [{"entity_id": "sensor.garage_weather_temperature", "state": state, "last_changed": iso(h_ago),
               "attributes": {}},
              {"entity_id": "sensor.garage_weather_battery", "state": state, "last_changed": iso(h_ago),
               "attributes": {}}]
    ents = [{"entity_id": s["entity_id"], "device_id": "d1", "platform": "mqtt", "disabled_by": None} for s in states]
    devs = [{"id": "d1", "name": "Garage Weather", "name_by_user": None}]
    return states, ents, devs


def test_silent_zigbee_device_is_flagged_for_household():
    r = checks.check_silent_devices(*_device_fixture("unavailable", 72), NOW)
    assert r["status"] == "WARN" and "Garage Weather" in r["reason"] and r["household"]


def test_recently_unavailable_or_alive_device_passes():
    assert checks.check_silent_devices(*_device_fixture("unavailable", 2), NOW)["status"] == "PASS"
    assert checks.check_silent_devices(*_device_fixture("21.4", 72), NOW)["status"] == "PASS"


def test_silent_exception_respected():
    r = checks.check_silent_devices(*_device_fixture("unavailable", 72), NOW, exceptions=["Garage Weather"])
    assert r["status"] == "PASS"


# ---- batteries: the ZB-02 case

def test_low_battery_and_phone_excluded():
    states = [
        {"entity_id": "sensor.front_door_battery", "state": "20",
         "attributes": {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Front door lock"}},
        {"entity_id": "sensor.phone_battery", "state": "5",
         "attributes": {"device_class": "battery", "unit_of_measurement": "%"}},
    ]
    ents = [{"entity_id": "sensor.front_door_battery", "platform": "mqtt"},
            {"entity_id": "sensor.phone_battery", "platform": "mobile_app"}]
    r = checks.check_batteries(states, ents)
    assert r["status"] == "WARN" and "Front door lock" in r["household"]
    states[0]["state"] = "8"
    assert checks.check_batteries(states, ents)["status"] == "FAIL"


# ---- supervisor / watched entity: the HVAC-06 case

def test_supervisor():
    assert checks.check_supervisor({"unhealthy": [], "unsupported": []})["status"] == "PASS"
    assert checks.check_supervisor({"unhealthy": ["docker"], "unsupported": []})["status"] == "FAIL"


def test_watched_estate_fault():
    rule = {"entity": "sensor.alarm_status", "bad": ["Fault"], "for_min": 60}
    st = [{"entity_id": "sensor.alarm_status", "state": "Fault", "last_changed": iso(3)}]
    assert checks.check_watched_entity(st, rule, NOW)["status"] == "FAIL"
    st[0]["last_changed"] = iso(0.25)
    assert checks.check_watched_entity(st, rule, NOW)["status"] == "WARN"
    st[0]["state"] = "Normal"
    assert checks.check_watched_entity(st, rule, NOW)["status"] == "PASS"
