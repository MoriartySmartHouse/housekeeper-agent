"""alarms.events: alarm episodes between hourly runs, from history + the 'Alarm:' logbook lines (facts only)."""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import agent
import checks

MDT = timedelta(hours=-6)


def local(day, hms):
    """A wall-clock time in Edmonton (MDT, UTC-6) in October 2026 -> aware UTC datetime."""
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC) - MDT


NOW = local(12, "21:00:00")
REG = [{"entity_id": "input_select.hvac_status", "labels": ["alarm_process"]},
       {"entity_id": "input_select.sump_status", "labels": ["alarm_process"]},
       {"entity_id": "input_select.sauna_heat_status", "labels": ["alarm_process"], "disabled_by": "user"},
       {"entity_id": "sensor.radon", "labels": []}]
STATES = [{"entity_id": "input_select.hvac_status", "state": "Normal", "attributes": {"friendly_name": "HVAC Status"}},
          {"entity_id": "input_select.sump_status", "state": "Normal", "attributes": {"friendly_name": "Sump Status"}}]

# 2026-10-12, modelled on the real day: the overnight furnace failure (as the 10-12 replayed alarms would have
# raised it), Fault <-> Normal three times in the afternoon, the false 'losing heat' Critical 15:14 and the false
# 'no heat' Critical at 20:15 that lasted 42 s.
HVAC = [("Critical", 11, "22:01:00", 12, "00:31:00", "Furnace short cycling: 4 starts in 30 min"),
        ("Critical", 12, "05:58:00", 12, "09:21:00", "Heat not delivered: Nest calling 22 min, house +0.02 C"),
        ("Fault", 12, "14:11:20", 12, "14:16:05", "Upstairs temperature source stale"),
        ("Fault", 12, "14:22:40", 12, "14:31:10", "Upstairs temperature source stale"),
        ("Critical", 12, "15:14:01", 12, "15:16:39", "House losing heat: -2.1 C in 2 h while calling"),
        ("Fault", 12, "15:25:30", 12, "15:33:00", "Upstairs temperature source stale"),
        ("Critical", 12, "20:15:10", 12, "20:15:52", "Heat not delivered: Nest calling 13 min, house +0.04 C")]
WORD = {"Critical": "CRITICAL", "Fault": "FAULT", "Warn": "WARN", "Tripped": "TRIP"}


def fixture():
    """-> (history, logbook events) as HA's websocket returns them (compressed rows; logbook `when` as epoch)."""
    rows = [{"s": "Normal", "lu": (NOW - timedelta(hours=24)).timestamp()}]
    log = []
    for tier, d1, t1, d2, t2, msg in HVAC:
        start, end = local(d1, t1), local(d2, t2)
        rows += [{"s": tier, "lu": start.timestamp()}, {"s": "Normal", "lu": end.timestamp()}]
        log.append({"name": "Alarm: HVAC", "message": f"{WORD[tier]} - {msg}", "when": start.timestamp() + 1})
        if tier == "Critical" and end - start > timedelta(minutes=15):      # Criticals re-push every 15 min
            log.append({"name": "Alarm: HVAC", "message": f"CRITICAL - {msg}",
                        "when": (start + timedelta(minutes=15)).timestamp()})
        log.append({"name": "Alarm: HVAC", "message": "CLEAR - cleared", "when": end.timestamp() + 1})
    log.append({"name": "Alarm: Radon", "message": "WARN - 160 Bq/m3", "when": local(12, "10:00:00").timestamp()})
    log.append({"name": "Front door", "message": "opened", "when": local(12, "10:01:00").timestamp()})
    history = {"input_select.hvac_status": rows,
               "input_select.sump_status": [{"s": "Normal", "lu": (NOW - timedelta(days=3)).timestamp()}]}
    return history, log


def run(history=None, log=None, memory=None, states=STATES, now=NOW):
    h, lg = fixture()
    history = h if history is None else history
    log = lg if log is None else log
    logbook = None if log == "fail" else {"events": log, "to": now.isoformat()}
    return checks.check_alarm_events(states, checks.alarm_entities(REG), history, logbook, now,
                                     {} if memory is None else memory)


def test_labelled_processes_and_their_keys():
    assert checks.alarm_entities(REG) == {"input_select.hvac_status": "hvac", "input_select.sump_status": "sump"}
    assert checks.alarm_process_key("Sauna Heat") == "sauna_heat"
    assert checks.alarm_process_key("Furnace Short-Cycle") == "furnace_short_cycle"


def test_the_12th_every_episode_with_times_and_messages():
    r = run()
    ev = r["evidence"]
    assert r["status"] == "PASS" and ev["v"] == 1 and ev["messages"] == "read" and ev["truncated"] == 0
    eps = ev["episodes"]
    assert [(e["tier"], e["s"]) for e in eps] == [("Critical", 9000), ("Critical", 12180), ("Fault", 285),
                                                  ("Fault", 510), ("Critical", 158), ("Fault", 450),
                                                  ("Critical", 42)]
    last = eps[-1]
    assert last["process"] == "HVAC" and last["start"] == local(12, "20:15:10").isoformat()
    assert last["end"] == local(12, "20:15:52").isoformat() and last["src"] == "history"
    assert last["msg"] == "Heat not delivered: Nest calling 13 min, house +0.04 C"
    assert eps[0]["msg"] == "Furnace short cycling: 4 starts in 30 min"
    hvac = ev["processes"]["HVAC"]
    assert hvac == {"count": 7, "worst": "Critical", "not_normal_s": 9000 + 12180 + 285 + 510 + 158 + 450 + 42,
                    "active": None}
    assert "Sump" not in ev["processes"]                       # watched, nothing happened
    assert "Radon" not in str(ev) and "Front door" not in str(ev)   # not a labelled process / not an alarm line
    assert r["reason"] == "7 alarm episode(s) in 24 h: HVAC 7x (worst Critical)"


def test_still_on_is_open_and_active():
    h, _ = fixture()
    h["input_select.sump_status"].append({"s": "Tripped", "lu": (NOW - timedelta(minutes=30)).timestamp()})
    ev = run(history=h)["evidence"]
    sump = [e for e in ev["episodes"] if e["process"] == "Sump"]
    assert sump[0]["end"] is None and sump[0]["s"] == 1800
    assert ev["processes"]["Sump"]["active"] == "Tripped"


def test_tier_changes_stay_one_episode_and_restarts_do_not_split_it():
    t0 = (NOW - timedelta(hours=2)).timestamp()
    rows = [{"s": "Normal", "lu": t0 - 60}, {"s": "Fault", "lu": t0}, {"s": "unavailable", "lu": t0 + 100},
            {"s": "Fault", "lu": t0 + 160}, {"s": "Critical", "lu": t0 + 300}, {"s": "Warn", "lu": t0 + 600},
            {"s": "Normal", "lu": t0 + 900}]
    [ep] = run(history={"input_select.hvac_status": rows}, log=[])["evidence"]["episodes"]
    assert ep["tier"] == "Critical" and ep["s"] == 900
    assert ep["tiers"] == {"Fault": 300, "Critical": 300, "Warn": 300}


def test_episode_running_at_window_start_is_flagged():
    rows = [{"s": "Fault", "lu": (NOW - timedelta(hours=24)).timestamp(),
             "lc": (NOW - timedelta(hours=30)).timestamp()},
            {"s": "Normal", "lu": (NOW - timedelta(hours=23)).timestamp()}]
    r = run(history={"input_select.hvac_status": rows}, log=[])
    [ep] = r["evidence"]["episodes"]
    assert ep["before_window"] and ep["s"] == 7 * 3600
    assert r["evidence"]["processes"]["HVAC"]["not_normal_s"] == 3600   # time inside the window only


def test_logbook_is_a_second_witness_when_history_dropped_rows():
    """LL-24: history can drop rows. An annunciation with no state change behind it still counts."""
    _, log = fixture()
    t = local(12, "03:00:00").timestamp()
    log += [{"name": "Alarm: Sump", "message": "TRIP - Level HiHi 95%", "when": t},
            {"name": "Alarm: Sump", "message": "TRIP - Level HiHi 95%", "when": t + 900},
            {"name": "Alarm: Sump", "message": "CLEAR - level normal", "when": t + 1500}]
    ev = run(log=log)["evidence"]
    [sump] = [e for e in ev["episodes"] if e["process"] == "Sump"]
    assert sump["src"] == "logbook" and sump["tier"] == "Tripped" and sump["s"] == 1500
    assert sump["msg"] == "Level HiHi 95%"
    # with no CLEAR the end is unknown, not "still on"
    ev = run(log=log[:-1])["evidence"]
    [sump] = [e for e in ev["episodes"] if e["process"] == "Sump"]
    assert sump["end"] is None and sump["s"] is None and ev["processes"]["Sump"]["active"] is None


def test_logbook_lines_matching_history_add_no_episodes():
    assert all(e["src"] == "history" for e in run()["evidence"]["episodes"])


def test_logbook_read_incrementally_and_remembered():
    mem = {}
    first = run(memory=mem)
    assert mem["to"] == NOW.isoformat() and len(mem["entries"]) == len([x for x in fixture()[1]
                                                                          if x["name"] == "Alarm: HVAC"])
    assert checks.alarm_log_since({"alarm_log": mem}, NOW) == NOW - timedelta(minutes=5)
    assert checks.alarm_log_since({}, NOW) == NOW - timedelta(hours=24)
    # next hour: only the last hour was read (nothing new), the messages still come from memory
    later = NOW + timedelta(hours=1)
    again = run(log=[], memory=mem, now=later)
    assert [e["msg"] for e in again["evidence"]["episodes"]] == [e["msg"] for e in first["evidence"]["episodes"]]


def test_failed_logbook_read_keeps_states_and_does_not_move_on():
    mem = {"to": (NOW - timedelta(hours=3)).isoformat(), "entries": [["junk"], 5]}
    r = run(log="fail", memory=mem)
    assert r["status"] == "PASS" and len(r["evidence"]["episodes"]) == 7
    assert r["evidence"]["messages"] == "unread" and r["reason"].endswith("alarm messages not read this run")
    assert all(e["msg"] is None for e in r["evidence"]["episodes"])
    assert mem["to"] == (NOW - timedelta(hours=3)).isoformat() and mem["entries"] == []   # damaged lines dropped


def test_unreadable_is_unknown_and_no_alarm_processes_is_pass():
    assert checks.check_alarm_events([], None, {}, None, NOW)["status"] == "UNKNOWN"
    assert checks.alarm_entities([{"entity_id": "a.b"}]) is None                 # registry lost its labels field
    assert checks.check_alarm_events([], checks.alarm_entities(REG), None, None, NOW)["status"] == "UNKNOWN"
    r = checks.check_alarm_events([], {}, None, None, NOW)
    assert r["status"] == "PASS" and r["evidence"]["episodes"] == []
    quiet = run(history={"input_select.hvac_status": [{"s": "Normal", "lu": NOW.timestamp() - 9e4}]}, log=[])
    assert quiet["reason"] == "no alarms in 24 h (2 processes watched)"


def test_flapping_is_capped_but_criticals_and_totals_survive():
    t0 = (NOW - timedelta(hours=20)).timestamp()
    rows = [{"s": "Normal", "lu": t0 - 1}, {"s": "Critical", "lu": t0}, {"s": "Normal", "lu": t0 + 60}]
    for i in range(200):
        rows += [{"s": "Fault", "lu": t0 + 100 + i * 60}, {"s": "Normal", "lu": t0 + 130 + i * 60}]
    ev = run(history={"input_select.hvac_status": rows}, log=[])["evidence"]
    assert len(ev["episodes"]) == checks.ALARM_MAX_EPISODES and ev["truncated"] == 201 - checks.ALARM_MAX_EPISODES
    assert ev["episodes"][0]["tier"] == "Critical"
    assert ev["processes"]["HVAC"]["count"] == 201 and ev["processes"]["HVAC"]["not_normal_s"] == 60 + 200 * 30


def test_gather_reads_history_and_logbook_only_when_processes_exist(monkeypatch):
    calls = []

    class FakeCore:
        def call(self, msg_type, **kw):
            calls.append((msg_type, sorted(kw)))
            return {} if msg_type.startswith("history") else []
    monkeypatch.setattr(agent, "load_memory", lambda: {})
    data = {"entity_registry": REG}
    agent.gather_alarms(FakeCore(), data, {})
    assert [c[0] for c in calls] == ["history/history_during_period", "logbook/get_events"]
    assert data["alarm_logbook"]["events"] == [] and data["alarm_history"] == {}
    calls.clear()
    agent.gather_alarms(FakeCore(), {"entity_registry": [{"entity_id": "a.b", "labels": []}]}, {})
    assert calls == []


def test_card_with_alarms_stays_small():
    import json
    _, log = fixture()
    for i in range(400):
        log.append({"name": "Alarm: HVAC", "message": "FAULT - " + "x" * 400, "when": NOW.timestamp() - i * 100})
    assert len(json.dumps(run(log=log))) < 40_000
