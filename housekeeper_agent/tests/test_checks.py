import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import checks

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)


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
    def offsite(h):
        return checks.check_backup_offsite({"backups": [{"date": iso(h), "agent_ids": ["cloud.cloud"]}]}, NOW)
    assert offsite(30)["status"] == "WARN"
    assert offsite(80)["status"] == "FAIL"


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


# ---- review fixes (Fable review 2026-10-08)

def test_stale_device_flagged_when_z2m_availability_off():
    """HIGH-1: availability off -> dead device keeps its last value; must not PASS."""
    states, ents, devs = _device_fixture("21.4", 72)
    for s in states:
        s["last_reported"] = iso(72)
    r = checks.check_silent_devices(states, ents, devs, NOW)
    assert r["status"] == "WARN" and "not heard" in r["reason"] and r["audience"] == "operator"


def test_missing_timestamps_do_not_crash():
    states, ents, devs = _device_fixture("unavailable", 72)
    for s in states:
        s.pop("last_changed")
    assert checks.check_silent_devices(states, ents, devs, NOW)["status"] == "PASS"


def test_addon_offsite_backup_is_unknown_not_fail():
    info = {"backups": [{"date": iso(1), "agents": {"hassio.local": {}}}]}
    r = checks.check_backup_offsite(info, NOW, states=[{"entity_id": "sensor.backup_state", "state": "backed_up"}])
    assert r["status"] == "UNKNOWN"


def test_first_backup_in_progress_is_not_fail():
    info = {"last_attempted_automatic_backup": iso(0.5), "last_completed_automatic_backup": None}
    assert checks.check_backup_last_attempt(info, NOW)["status"] == "PASS"


def test_notification_titles_never_leave_the_house():
    r = checks.check_notifications([{"notification_id": "x", "title": "Steve arrived home"}])
    assert "Steve" not in str(r)
