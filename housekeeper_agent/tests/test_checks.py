import json
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


def _auto(eid, state="on", last=None, name=None):
    return {"entity_id": eid, "state": state,
            "attributes": {"friendly_name": name or eid, "last_triggered": last and last.isoformat()}}


def test_automation_stopped_is_learned_then_flagged():
    t0 = datetime(2026, 9, 1, 8, tzinfo=UTC)
    mem = {}
    # Driveway Alert fires daily; the leak alarm fired once months ago; one automation is broken.
    for day in range(6):
        now = t0 + timedelta(days=day, hours=1)
        states = [_auto("automation.driveway", last=t0 + timedelta(days=day), name="Driveway Alert"),
                  _auto("automation.leak", last=datetime(2026, 3, 1, tzinfo=UTC))]
        r = checks.check_automations(states, now, mem)
        assert r["status"] == "PASS"
    assert len(mem["automation.driveway"]) == 6 and len(mem["automation.leak"]) == 1
    # 3 days quiet: under 4x a 1-day gap -> fine; 5 days quiet -> stopped
    last = t0 + timedelta(days=5)
    states = [_auto("automation.driveway", last=last, name="Driveway Alert"),
              _auto("automation.leak", last=datetime(2026, 3, 1, tzinfo=UTC))]
    assert checks.check_automations(states, last + timedelta(days=3), mem)["status"] == "PASS"
    r = checks.check_automations(states, last + timedelta(days=5), mem)
    assert r["status"] == "WARN" and "Driveway Alert (silent 5.0 d, usually every 24 h)" in r["reason"]
    assert "leak" not in r["reason"]                       # one trigger on record: still learning, never flagged
    assert r["evidence"]["stopped"] == [{"name": "Driveway Alert", "silent_d": 5.0, "usual_gap_h": 24.0}]
    assert "2026-" not in json.dumps(r["evidence"])        # trigger times never leave the house
    assert checks.check_automations(states, last + timedelta(days=5), mem,
                                    exceptions=["Driveway Alert"])["status"] == "PASS"


def test_automation_unavailable_and_disabled():
    mem = {"automation.off": ["2026-09-01T00:00:00+00:00"]}
    now = datetime(2026, 10, 9, tzinfo=UTC)
    r = checks.check_automations([_auto("automation.bad", state="unavailable", name="Broken one"),
                                  _auto("automation.off", state="off")], now, mem)
    assert r["status"] == "WARN" and "1 not loaded (unavailable): Broken one" in r["reason"]
    assert "automation.off" not in mem                     # disabled on purpose: forgotten
    assert checks.check_automations([], now, {})["status"] == "UNKNOWN"
    assert checks.check_automations(None, now, {})["status"] == "UNKNOWN"


def test_review_restored_placeholders_and_bad_memory():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    starting = {"entity_id": "automation.x", "state": "unavailable", "attributes": {"restored": True}}
    mem = {"automation.y": "garbage", "automation.z": ["not a time"]}
    r = checks.check_automations([starting, _auto("automation.y", last=now), _auto("automation.z", last=now)], now, mem)
    assert r["status"] == "PASS"                                  # HA starting up is not "not loaded"
    assert mem["automation.y"] == [now.isoformat()] and mem["automation.z"] == [now.isoformat()]


def test_review_core_update_kept_when_many_waiting():
    states = [{"entity_id": f"update.addon_{i:02d}", "state": "on", "attributes": {}} for i in range(40)]
    states.append({"entity_id": "update.home_assistant_core_update", "state": "on",
                   "attributes": {"installed_version": "2026.10.0", "latest_version": "2026.10.1"}})
    assert "update.home_assistant_core_update" in checks.check_updates(states)["evidence"]["versions"]


def test_disk_thresholds_and_unknown():
    assert checks.check_disk({"disk_total": 30.8, "disk_free": 14.1})["status"] == "PASS"
    r = checks.check_disk({"disk_total": 30.8, "disk_free": 2.0})        # the 2026-10-09 state
    assert r["status"] == "WARN" and r["evidence"]["free_gb"] == 2.0 and "2.0 GB free" in r["reason"]
    r = checks.check_disk({"disk_total": 30.8, "disk_free": 1.0})
    assert r["status"] == "FAIL" and r["audience"] == "household"
    assert checks.check_disk({"disk_total": 0, "disk_free": 1})["status"] == "UNKNOWN"
    assert checks.check_disk(None)["status"] == "UNKNOWN"


def _plug(eid, dev, state, watts, on_at, zero_at, name=None):
    reg = [{"entity_id": eid, "device_id": dev}, {"entity_id": f"sensor.{dev}_power", "device_id": dev}]
    sts = [{"entity_id": eid, "state": state, "last_changed": on_at.isoformat(),
            "attributes": {"friendly_name": name or eid}},
           {"entity_id": f"sensor.{dev}_power", "state": str(watts), "last_changed": zero_at.isoformat(),
            "attributes": {"device_class": "power", "unit_of_measurement": "W"}}]
    return reg, sts


def test_zero_power_waits_hours_then_flags():
    t0 = datetime(2026, 10, 9, 8, tzinfo=UTC)
    reg, sts = _plug("switch.gym_fan", "d1", "on", 0.0, t0, t0, "Gym Fan")
    reg2, sts2 = _plug("switch.lamp", "d2", "on", 42.5, t0, t0)            # drawing power: fine
    reg3, sts3 = _plug("switch.off_one", "d3", "off", 0.0, t0, t0)         # off at 0 W: fine
    reg4 = [{"entity_id": "switch.no_meter", "device_id": "d4"}]           # no meter: not watched
    sts4 = [{"entity_id": "switch.no_meter", "state": "on", "attributes": {}}]
    R, S = reg + reg2 + reg3 + reg4, sts + sts2 + sts3 + sts4
    mem = {}
    r = checks.check_zero_power(S, R, t0 + timedelta(minutes=20), mem)    # Kasa slow to show watts: no flag
    assert r["status"] == "PASS" and r["evidence"]["switches_with_meter"] == 3
    r = checks.check_zero_power(S, R, t0 + timedelta(hours=7), mem)
    assert r["status"] == "WARN" and "Gym Fan (7 h)" in r["reason"]
    # HA restart resets last_changed; memory keeps the real start
    _, sts_r = _plug("switch.gym_fan", "d1", "on", 0.0, t0 + timedelta(hours=8), t0 + timedelta(hours=8), "Gym Fan")
    r = checks.check_zero_power(sts_r + sts2 + sts3 + sts4, R, t0 + timedelta(hours=9), mem)
    assert "Gym Fan (9 h)" in r["reason"]
    # power comes back -> forgotten
    _, sts_ok = _plug("switch.gym_fan", "d1", "on", 39.0, t0, t0, "Gym Fan")
    assert checks.check_zero_power(sts_ok + sts2 + sts3 + sts4, R, t0 + timedelta(hours=10), mem)["status"] == "PASS"
    assert "switch.gym_fan" not in mem
    assert checks.check_zero_power(S, R, t0 + timedelta(hours=30), {}, exceptions=["Gym Fan"])["status"] == "PASS"
    assert checks.check_zero_power(None, R, t0)["status"] == "UNKNOWN"


def test_zero_power_strip_outlets_on_one_device():
    """HS300 as seen live 2026-10-10: strip switch + outlet switches + their meters all on one device."""
    t0 = datetime(2026, 10, 9, 8, tzinfo=UTC)
    p = "tp_link_power_strip_7a93"
    reg = [{"entity_id": e, "device_id": "s"} for e in (
        f"switch.{p}", f"switch.{p}_plug_2", f"switch.{p}_plug_4", f"switch.{p}_led",
        f"sensor.{p}_current_consumption", f"sensor.{p}_plug_2_current_consumption",
        f"sensor.{p}_plug_4_current_consumption")]
    reg[3]["entity_category"] = "config"

    def sw(eid, state, name):
        return {"entity_id": eid, "state": state, "last_changed": t0.isoformat(), "attributes": {"friendly_name": name}}

    def pw(eid, w):
        return {"entity_id": eid, "state": str(w), "last_changed": t0.isoformat(),
                "attributes": {"device_class": "power", "unit_of_measurement": "W"}}

    sts = [sw(f"switch.{p}", "on", "Strip"), sw(f"switch.{p}_plug_2", "on", "Strip Plug 2"),
           sw(f"switch.{p}_plug_4", "on", "Strip Plug 4"), sw(f"switch.{p}_led", "on", "Strip LED"),
           pw(f"sensor.{p}_current_consumption", 30.0), pw(f"sensor.{p}_plug_2_current_consumption", 0.0),
           pw(f"sensor.{p}_plug_4_current_consumption", 30.0)]
    r = checks.check_zero_power(sts, reg, t0 + timedelta(hours=20), {})
    # only the idle outlet, measured by its own meter; strip switch and LED not listed
    assert r["status"] == "WARN" and r["reason"] == "1 switched on but drawing nothing: Strip Plug 2 (20 h)"
    assert r["evidence"]["switches_with_meter"] == 2



def test_zero_power_strip_outlet_meters_on_child_devices():
    """HS300 as seen live 2026-10-10 (all 18 outlets): each outlet's switch sits on the strip's device, but its meter
    sits on a child device of its own; they pair by unique_id (<switch uid>_current_power_w), not by device."""
    t0 = datetime(2026, 10, 9, 8, tzinfo=UTC)
    strip, mac = "switch.tp_link_power_strip_7a93", "30:68:93:BE:7A:93"
    names = ["Kitchen Fan", "Gym Fan", "Desk Lamp", "Printer", "Router", "Fridge"]
    objs = [n.lower().replace(" ", "_") for n in names]
    reg = [{"entity_id": strip, "device_id": "s", "unique_id": mac},
           {"entity_id": "sensor.tp_link_power_strip_7a93_current_consumption", "device_id": "s",
            "unique_id": f"{mac}_current_power_w"}]
    for i, o in enumerate(objs):
        reg += [{"entity_id": f"switch.{o}", "device_id": "s", "unique_id": f"X0{i}"},
                {"entity_id": f"sensor.{o}_current_consumption", "device_id": f"c{i}",
                 "unique_id": f"X0{i}_current_power_w"},
                {"entity_id": f"sensor.{o}_today_s_consumption", "device_id": f"c{i}",
                 "unique_id": f"X0{i}_today_energy_kwh"}]

    def sw(eid, state, name):
        return {"entity_id": eid, "state": state, "last_changed": t0.isoformat(), "attributes": {"friendly_name": name}}

    def pw(eid, w):
        return {"entity_id": eid, "state": str(w), "last_changed": t0.isoformat(),
                "attributes": {"device_class": "power", "unit_of_measurement": "W"}}

    watts = [12.0, 30.0, 0.0, 5.0, 8.0, 0.0]          # Desk Lamp is off at 0 W (fine); Fridge on at 0 W
    states = ["on", "on", "off", "on", "on", "on"]
    sts = [sw(strip, "on", "TP-LINK_Power Strip_7A93"), pw("sensor.tp_link_power_strip_7a93_current_consumption", 55.0)]
    for o, n, w, st in zip(objs, names, watts, states, strict=True):
        sts += [sw(f"switch.{o}", st, n), pw(f"sensor.{o}_current_consumption", w),
                {"entity_id": f"sensor.{o}_today_s_consumption", "state": "0.4", "attributes": {}}]
    r = checks.check_zero_power(sts, reg, t0 + timedelta(hours=20), {})
    # exactly the idle outlet, by its own name; the strip switch (on, its total drawing) is not listed
    assert r["status"] == "WARN" and r["reason"] == "1 switched on but drawing nothing: Fridge (20 h)"
    assert r["evidence"]["switches_with_meter"] == 6
    sts = [s for s in sts if s["entity_id"] != "sensor.fridge_current_consumption"] + [
        pw("sensor.fridge_current_consumption", 90.0)]
    assert checks.check_zero_power(sts, reg, t0 + timedelta(hours=20), {})["status"] == "PASS"
    # everything on at 0 W, strip total too: the six outlets are listed, never the strip's main switch beside them
    dead = [dict(s, state="on") if s["entity_id"].startswith("switch.") else
            dict(s, state="0.0") if s["entity_id"].endswith("_current_consumption") else s for s in sts]
    r = checks.check_zero_power(dead, reg, t0 + timedelta(hours=20), {})
    assert sorted(z["name"] for z in r["evidence"]["zero_power"]) == sorted(names)

def test_integrations_failing_twice_in_a_row():
    entries = [{"entry_id": "a", "domain": "wled", "title": "Entertainment Stand", "state": "setup_retry"},
               {"entry_id": "b", "domain": "tplink", "title": "Computer Plug", "state": "setup_error"},
               {"entry_id": "c", "domain": "wiz", "title": "Kitchen1", "state": "loaded"},
               {"entry_id": "d", "domain": "sonos", "title": "sonos", "state": "not_loaded", "source": "ignore"},
               {"entry_id": "e", "domain": "matter", "title": "Matter", "state": "setup_error", "disabled_by": "user"}]
    mem = {}
    r = checks.check_integrations(entries, mem)
    assert r["status"] == "PASS" and set(mem) == {"a", "b"}            # first sighting: wait an hour
    assert r["reason"] == "1 of 3 integrations loaded; 2 not loading, checked again next run"
    r = checks.check_integrations(entries, mem)
    assert r["status"] == "WARN" and r["reason"].startswith("2 integration(s) not working: tplink: Computer Plug")
    assert "wled: Entertainment Stand (setup retry)" in r["reason"] and r["evidence"]["integrations"] == 3
    entries[0]["state"] = "loaded"
    r = checks.check_integrations(entries, mem, exceptions=["tplink"])
    assert r["status"] == "PASS" and mem == {}
    assert checks.check_integrations(None)["status"] == "UNKNOWN"


def test_log_errors_by_integration_counts_only():
    now = datetime(2026, 10, 9, 20, tzinfo=UTC)
    recent, old = (now - timedelta(hours=2)).timestamp(), (now - timedelta(days=3)).timestamp()
    log = [{"name": "homeassistant.components.wiz.light", "level": "ERROR", "timestamp": recent, "count": 40,
            "message": ["Kitchen3 unreachable 192.168.0.81"]},
           {"name": "custom_components.tapo_control", "level": "ERROR", "timestamp": recent, "count": 3},
           {"name": "homeassistant.components.mqtt", "level": "WARNING", "timestamp": recent, "count": 99},
           {"name": "homeassistant.components.kasa", "level": "ERROR", "timestamp": old, "count": 500},
           {"name": "homeassistant.core", "level": "ERROR", "timestamp": recent, "count": 1}]
    r = checks.check_log_errors(log, now)
    assert r["status"] == "WARN" and r["reason"] == "44 errors in HA's log in 24 h: wiz 40x"
    assert r["evidence"]["by_integration"] == {"wiz": 40, "tapo_control": 3, "homeassistant.core": 1}
    assert "192.168" not in json.dumps(r)                               # never the log text
    assert checks.check_log_errors([], now)["status"] == "PASS"
    assert checks.check_log_errors(None, now)["status"] == "UNKNOWN"
