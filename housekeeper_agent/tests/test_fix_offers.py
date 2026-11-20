"""Fix offers (Watson v2 part 3, B-0170): evidence.fixable, the fix_offers option, the sensor attribute, the echo."""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import agent
import checks

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
OLD = (NOW - timedelta(hours=72)).isoformat()


def test_silent_esphome_device_is_fixable_but_not_zigbee():
    reg = [{"entity_id": "sensor.garage_temp", "device_id": "e", "platform": "esphome"},
           {"entity_id": "sensor.attic_temp", "device_id": "z", "platform": "mqtt"}]
    devs = [{"id": "e", "name": "Garage Weather"}, {"id": "z", "name": "Attic"}]
    states = [{"entity_id": "sensor.garage_temp", "state": "unavailable", "last_changed": OLD},
              {"entity_id": "sensor.attic_temp", "state": "unavailable", "last_changed": OLD}]
    r = checks.check_silent_devices(states, reg, devs, NOW)
    assert r["status"] == "WARN" and len(r["evidence"]["silent"]) == 2
    assert r["evidence"]["fixable"] == [{"kind": "reload_entry", "entity_id": "sensor.garage_temp",
                                         "label": "Garage Weather"}]


def _flap_rows(n):
    rows = []
    for i in range(n):
        t = NOW - timedelta(hours=20 - i)
        rows += [{"s": "on", "lu": t.timestamp()}, {"s": "unavailable", "lu": (t + timedelta(minutes=1)).timestamp()},
                 {"s": "on", "lu": (t + timedelta(minutes=20)).timestamp()}]
    return rows


def test_flapping_wifi_device_is_fixable_not_mesh_lock_or_unknown_platform():
    cands = {"light.kitchen3": "Kitchen3", "light.z_bulb": "Zigbee bulb", "lock.front": "Front door",
             "switch.mystery": "Mystery"}
    hist = {e: _flap_rows(4) for e in cands}
    plats = {"light.kitchen3": "wiz", "light.z_bulb": "zha", "lock.front": "august"}
    r = checks.check_flapping(hist, cands, platforms=plats)
    assert r["status"] == "WARN" and len(r["evidence"]["flapping"]) == 4
    assert r["evidence"]["fixable"] == [{"kind": "reload_entry", "entity_id": "light.kitchen3", "label": "Kitchen3"}]
    assert "fixable" not in checks.check_flapping(hist, cands)["evidence"]     # no registry: nothing listed


def test_stuck_integration_is_fixable_only_when_retrying_and_not_mesh():
    entries = [{"entry_id": "01WIZ", "domain": "wiz", "title": "Kitchen4", "state": "setup_retry"},
               {"entry_id": "01ZHA", "domain": "zha", "title": "Zigbee", "state": "setup_retry"},
               {"entry_id": "01MIG", "domain": "hue", "title": "Hue", "state": "migration_error"}]
    mem = {}
    assert checks.check_integrations(entries, mem)["evidence"]["fixable"] == []   # first sighting: not yet
    r = checks.check_integrations(entries, mem)
    assert r["status"] == "WARN"
    assert r["evidence"]["fixable"] == [{"kind": "reload_stuck", "entry_id": "01WIZ", "label": "wiz: Kitchen4"}]


def test_zero_power_switch_is_fixable():
    reg = [{"entity_id": "switch.plug", "device_id": "p", "unique_id": "U1", "platform": "tplink"},
           {"entity_id": "sensor.plug_power", "device_id": "p", "unique_id": "U1_current_power_w"}]
    old = (NOW - timedelta(hours=8)).isoformat()
    states = [{"entity_id": "switch.plug", "state": "on", "last_changed": old, "attributes": {"friendly_name": "Plug"}},
              {"entity_id": "sensor.plug_power", "state": "0.0", "last_changed": old,
               "attributes": {"device_class": "power", "unit_of_measurement": "W"}}]
    r = checks.check_zero_power(states, reg, NOW, {})
    assert r["evidence"]["fixable"] == [{"kind": "outlet_off", "entity_id": "switch.plug", "label": "Plug"}]


def _flapping_house():
    reg = [{"entity_id": "light.kitchen3", "device_id": "k", "platform": "wiz", "labels": []}]
    return {"errors": {}, "states": [], "entity_registry": reg, "device_registry": [{"id": "k", "name": "Kitchen3"}],
            "flap_candidates": {"light.kitchen3": "Kitchen3"}, "history_24h": {"light.kitchen3": _flap_rows(4)}}


def test_option_off_keeps_every_fixable_list_at_home():
    out = agent.run_checks(_flapping_house(), {"watch": []}, NOW)
    assert "fixable" not in json.dumps(out) and "light.kitchen3" not in json.dumps(out)
    on = {c["id"]: c for c in agent.run_checks(_flapping_house(), {"watch": [], "fix_offers": True}, NOW)}
    assert on["devices.flapping"]["evidence"]["fixable"][0]["entity_id"] == "light.kitchen3"


def test_card_echoes_published_ids_only_with_the_option():
    assert "fix_offers_published" not in agent.build_card({"site_id": "x"}, "1", [], {}, NOW, ["abc"])
    card = agent.build_card({"site_id": "x", "fix_offers": True}, "1", [], {}, NOW, ["abc"])
    assert card["fix_offers_published"] == ["abc"]
    assert agent.build_card({"site_id": "x", "fix_offers": True}, "1", [], {}, NOW)["fix_offers_published"] == []


OFFER = {"id": "a1b2c3d4e5f60718", "kind": "reload_entry", "entity_id": "light.kitchen3", "label": "Kitchen3",
         "expires": "2026-11-21T02:00:00+00:00"}


def test_sensor_gets_only_valid_offers_and_only_with_the_option():
    reply = {"household": [], "fix_offers": [
        OFFER,
        {"id": "x", "kind": "reload_entry", "entity_id": "light.k", "expires": OFFER["expires"]},       # short id
        {"id": "b1b2c3d4e5f60718", "kind": "turn_on_heater", "entity_id": "switch.h", "expires": OFFER["expires"]},
        {"id": "c1b2c3d4e5f60718", "kind": "outlet_off", "entity_id": "switch.a; rm", "expires": OFFER["expires"]},
        {"id": "d1b2c3d4e5f60718", "kind": "reload_stuck", "entry_id": "01WIZ", "expires": "soon"},
        "junk"]}
    assert agent.fix_offers(reply, {}) == []
    assert agent.fix_offers(reply, {"fix_offers": True}) == [OFFER]
    assert agent.fix_offers(None, {"fix_offers": True}) == []                  # central unreachable: list empties
    body = agent.house_status(reply, [], "Watson", None, agent.fix_offers(reply, {"fix_offers": True}))
    assert body["attributes"]["fix_offers"] == [OFFER]
    assert agent.house_status(reply, [])["attributes"]["fix_offers"] == []


def test_cycle_writes_offers_and_remembers_them_for_the_next_card(monkeypatch, tmp_path):
    mem_file = str(tmp_path / "memory.json")
    real_load, real_save = agent.load_memory, agent.save_memory
    monkeypatch.setattr(agent, "load_memory", lambda path=None: real_load(mem_file))
    monkeypatch.setattr(agent, "save_memory", lambda mem, path=None: real_save(mem, mem_file))
    monkeypatch.setattr(agent, "gather", lambda token: ({"errors": {}, "states": None}, "2026.11.0"))
    sent = []

    def http(method, url, token=None, body=None, timeout=30):
        sent.append((method, url, body))
        return {"household": [], "fix_offers": [OFFER]} if url.endswith("/v1/report") else None
    monkeypatch.setattr(agent, "http", http)
    opts = {"site_id": "x", "central_url": "https://c.example", "site_key": "k", "watch": [], "fix_offers": True}
    card, _, status = agent.cycle(opts, "t")
    assert card["fix_offers_published"] == [] and status["attributes"]["fix_offers"] == [OFFER]
    card, _, _ = agent.cycle(opts, "t")
    assert card["fix_offers_published"] == [OFFER["id"]]
    assert all(m == "GET" or u.endswith(("/v1/report", f"/states/{agent.STATUS_ENTITY}")) for m, u, _ in sent)
    opts["fix_offers"] = False                                   # off: the sensor list empties, nothing is echoed
    card, _, status = agent.cycle(opts, "t")
    assert "fix_offers_published" not in card and status["attributes"]["fix_offers"] == []
    assert real_load(mem_file)["fix_offers_published"] == []
