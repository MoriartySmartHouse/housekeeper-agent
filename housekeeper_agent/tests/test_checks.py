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
    devs = [{"id": "d1", "name": "Garage Weather", "name_by_user": None,
             "identifiers": [["mqtt", "zigbee2mqtt_0x00158d0001a2b3c4"]], "via_device_id": "bridge"}]
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


# ---- 0.1.1: restart-proof silent detection (LL-50 — what fooled 0.1.0 on its first real run)

def test_restart_resets_ha_clock_but_memory_remembers():
    """Device dead for weeks; HA restarted 16 h ago so last_changed says 16 h. Memory says 10 days."""
    states, ents, devs = _device_fixture("unavailable", 16)
    memory = {"d1": iso(240)}
    r = checks.check_silent_devices(states, ents, devs, NOW, memory=memory)
    assert r["status"] == "WARN" and "10.0 d" in r["reason"]


def test_memory_starts_counting_and_drops_recovered_devices():
    states, ents, devs = _device_fixture("unavailable", 16)
    memory = {}
    r = checks.check_silent_devices(states, ents, devs, NOW, memory=memory)
    assert r["status"] == "PASS" and "under 24 h" in r["reason"] and "d1" in memory
    later = NOW + timedelta(hours=9)
    assert checks.check_silent_devices(states, ents, devs, later, memory=memory)["status"] == "WARN"
    states2, _, _ = _device_fixture("21.0", 0)
    checks.check_silent_devices(states2, ents, devs, later, memory=memory)
    assert "d1" not in memory


def test_z2m_last_seen_sensor_is_trusted_over_restarts():
    states, ents, devs = _device_fixture("21.0", 1)  # HA shows a fresh-looking value
    states.append({"entity_id": "sensor.garage_weather_last_seen", "state": iso(100), "last_changed": iso(1)})
    ents.append({"entity_id": "sensor.garage_weather_last_seen", "device_id": "d1", "platform": "mqtt"})
    r = checks.check_silent_devices(states, ents, devs, NOW)
    assert r["status"] == "WARN" and r["evidence"]["zigbee_without_last_seen"] == []


def test_lists_zigbee_devices_without_last_seen():
    r = checks.check_silent_devices(*_device_fixture("21.0", 1), NOW)
    assert r["evidence"]["zigbee_without_last_seen"] == ["Garage Weather"]


# ---- 0.1.2: what the first live report showed

def _mqtt_device(dev_id, name, ident, via=None):
    state = {"entity_id": f"sensor.{dev_id}_status", "state": "ok", "last_changed": iso(1),
             "last_reported": iso(1), "attributes": {}}
    ent = {"entity_id": state["entity_id"], "device_id": dev_id, "platform": "mqtt", "disabled_by": None}
    dev = {"id": dev_id, "name": name, "name_by_user": None, "identifiers": [["mqtt", ident]], "via_device_id": via}
    return state, ent, dev


def test_zigbee_without_last_seen_lists_only_z2m_devices():
    """Live case: the bridge and BirdNET-Go (other MQTT discovery) were listed as Zigbee devices."""
    fixtures = [_mqtt_device("bridge", "Zigbee2MQTT Bridge", "zigbee2mqtt_bridge_0x00124b0024c1d2e3"),
                _mqtt_device("bn", "BirdNET-Go", "birdnet-go"),
                _mqtt_device("bn1", "BirdNET-Go Stream 1", "birdnet-go_stream_1", via="bn"),
                _mqtt_device("d2", "Hall Motion", "zigbee2mqtt_0x00158d0001a2b3c5", via="bridge"),
                _mqtt_device("d3", "Porch Button", "some_future_id", via="bridge")]  # via the bridge counts
    states, ents, devs = _device_fixture("21.0", 1)
    states += [f[0] for f in fixtures]
    ents += [f[1] for f in fixtures]
    devs += [f[2] for f in fixtures]
    r = checks.check_silent_devices(states, ents, devs, NOW)
    assert r["status"] == "PASS" and r["evidence"]["devices_checked"] == 6
    assert r["evidence"]["zigbee_without_last_seen"] == ["Garage Weather", "Hall Motion", "Porch Button"]


def test_unavailable_under_threshold_are_named_in_evidence():
    states, ents, devs = _device_fixture("unavailable", 16)
    r = checks.check_silent_devices(states, ents, devs, NOW, memory={})
    assert r["status"] == "PASS" and "Garage Weather" not in r["reason"]
    assert r["evidence"]["unavailable_under_threshold"] == [{"name": "Garage Weather", "since": iso(16), "h": 16.0}]
    assert checks.check_silent_devices(*_device_fixture("unavailable", 72), NOW)["evidence"][
        "unavailable_under_threshold"] == []


def test_unavailable_under_threshold_list_is_bounded():
    states, ents, devs = [], [], []
    for n in range(30):
        st, en, dv = _mqtt_device(f"d{n}", f"Sensor {n}", f"zigbee2mqtt_0x{n:016x}")
        st.update(state="unavailable", last_changed=iso(n / 10))
        states.append(st), ents.append(en), devs.append(dv)
    r = checks.check_silent_devices(states, ents, devs, NOW, memory={})
    listed = r["evidence"]["unavailable_under_threshold"]
    assert r["evidence"]["watching_since_restart"] == 30 and len(listed) == 25
    assert listed[0]["name"] == "Sensor 29"  # longest-unavailable first


# ---- flapping devices (Session C; the humidifier plug dropped off 3x on 2026-10-09)

REG = [{"entity_id": "switch.humidifier", "device_id": "d1"},
       {"entity_id": "sensor.humidifier_power", "device_id": "d1"},
       {"entity_id": "button.humidifier_identify", "device_id": "d1", "entity_category": "config"},
       {"entity_id": "light.lamp", "device_id": "d2"},
       {"entity_id": "switch.lamp_led", "device_id": "d2", "disabled_by": "user"},
       {"entity_id": "sensor.only_temp", "device_id": "d3"}]
DEVS = [{"id": "d1", "name": "KP125M", "name_by_user": "Humidifier plug"}, {"id": "d2", "name": "Lamp"},
        {"id": "d3", "name": "Thermo"}]


def test_flap_candidates_one_per_device_no_sensors():
    assert checks.flap_candidates(REG, DEVS) == {"switch.humidifier": "Humidifier plug", "light.lamp": "Lamp"}
    assert checks.flap_candidates(None, DEVS) == {}


def test_flapping_counts_drops_not_samples():
    cands = checks.flap_candidates(REG, DEVS)
    seq = ["on", "unavailable", "off", "unavailable", "unavailable", "off", "unavailable", "off"]
    hist = {"switch.humidifier": [{"s": x} for x in seq],
            "light.lamp": [{"s": "on"}, {"s": "unavailable"}, {"s": "on"}]}
    r = checks.check_flapping(hist, cands)
    assert r["status"] == "WARN" and "Humidifier plug (3x)" in r["reason"]
    assert r["evidence"]["flapping"] == [{"name": "Humidifier plug", "drops_24h": 3}]
    assert checks.check_flapping({"light.lamp": [{"s": "on"}]}, cands)["status"] == "PASS"


def test_flapping_unreadable_is_unknown_and_starting_unavailable_is_not_a_drop():
    cands = checks.flap_candidates(REG, DEVS)
    assert checks.check_flapping(None, cands)["status"] == "UNKNOWN"
    hist = {"switch.humidifier": [{"s": "unavailable"}, {"s": "on"}, {"s": "unavailable"}, {"s": "on"}]}
    assert checks.check_flapping(hist, cands)["status"] == "PASS"   # 1 drop: the first row isn't a transition


def test_battery_levels_reported_for_trends():
    states = [{"entity_id": "sensor.lock_battery", "state": "62",
               "attributes": {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Lock battery"}},
              {"entity_id": "sensor.phone_battery", "state": "5",
               "attributes": {"device_class": "battery", "unit_of_measurement": "%"}}]
    reg = [{"entity_id": "sensor.phone_battery", "platform": "mobile_app"}]
    r = checks.check_batteries(states, reg)
    assert r["status"] == "PASS" and r["evidence"]["levels"] == {"Lock battery": 62}


def test_integration_domains_splits_platform_pairs():
    cfg = {"components": ["mqtt", "sensor.mqtt", "zha", "light", "light.wled", "", 5]}
    assert checks.integration_domains(cfg) == ["light", "mqtt", "sensor", "wled", "zha"]
    assert checks.integration_domains({}) is None
    assert checks.integration_domains(None) is None


def test_updates_carry_versions_and_integrations():
    states = [{"entity_id": "update.home_assistant_core_update", "state": "on",
               "attributes": {"installed_version": "2026.10.0", "latest_version": "2026.10.1"}},
              {"entity_id": "update.frigate", "state": "off", "attributes": {}}]
    r = checks.check_updates(states, ["mqtt", "zha"])
    assert r["status"] == "PASS"
    assert r["evidence"]["versions"] == {"update.home_assistant_core_update":
                                         {"installed": "2026.10.0", "latest": "2026.10.1"}}
    assert r["evidence"]["integrations"] == ["mqtt", "zha"]
    assert "integrations" not in checks.check_updates(states)["evidence"]   # unread config: just left out
