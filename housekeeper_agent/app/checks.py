"""Housekeeper checks — pure functions over data the agent has already read.

Each check returns a dict: {id, status, reason, household, audience, evidence}.
status is PASS / WARN / FAIL / UNKNOWN.

Rule (DEC-34): a check that cannot read or understand its source returns UNKNOWN,
never PASS. A breaking HA change must look like a grey check, not a healthy house.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise

PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"


def result(cid, status, reason, household=None, audience="operator", evidence=None):
    return {
        "id": cid,
        "status": status,
        "reason": reason,
        "household": household,
        "audience": audience if household else "operator",
        "evidence": evidence or {},
    }


def unknown(cid, why):
    return result(cid, UNKNOWN, f"cannot read source: {why}")


def parse_ts(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 1)


# ---------------------------------------------------------------- backups

LOCAL_AGENT_PREFIXES = ("hassio.", "backup.local")


def _backup_locations(backup):
    """Agent ids a backup is stored on. Handles `agents` (dict) and older `agent_ids` (list)."""
    if isinstance(backup.get("agents"), dict):
        return list(backup["agents"].keys())
    if isinstance(backup.get("agent_ids"), list):
        return list(backup["agent_ids"])
    return None


# Add-ons that copy backups off-site outside HA's backup agents (e.g. Google Drive Backup).
OFFSITE_ADDON_ENTITIES = ("sensor.backup_state",)


def check_backup_offsite(info, now, warn_h=26, fail_h=50, states=None):
    cid = "backup.offsite"
    if not isinstance(info, dict) or not isinstance(info.get("backups"), list):
        return unknown(cid, "backup/info has no 'backups' list")
    newest = None
    for b in info["backups"]:
        locs = _backup_locations(b)
        if locs is None:
            return unknown(cid, "backup entry has neither 'agents' nor 'agent_ids'")
        offsite = [a for a in locs if not a.startswith(LOCAL_AGENT_PREFIXES)]
        when = parse_ts(b.get("date"))
        if offsite and when and (newest is None or when > newest[0]):
            newest = (when, offsite)
    if newest is None:
        addon = [s.get("entity_id") for s in states or [] if s.get("entity_id") in OFFSITE_ADDON_ENTITIES]
        if addon:
            return unknown(cid, f"off-site copy is handled by an add-on ({', '.join(addon)}), not yet read")
        return result(cid, FAIL, "no backup stored off-site",
                      "Your home has no backup stored outside the house.", "household")
    age = hours(now - newest[0])
    ev = {"newest_offsite": newest[0].isoformat(), "age_h": age, "locations": newest[1]}
    if age < warn_h:
        return result(cid, PASS, f"off-site backup {age} h old ({', '.join(newest[1])})", evidence=ev)
    status = WARN if age < fail_h else FAIL
    return result(cid, status, f"newest off-site backup is {age} h old", evidence=ev)


def check_backup_last_attempt(info, now, grace_h=2):
    cid = "backup.last_attempt"
    if not isinstance(info, dict) or "last_attempted_automatic_backup" not in info:
        return unknown(cid, "backup/info has no 'last_attempted_automatic_backup'")
    attempted = parse_ts(info.get("last_attempted_automatic_backup"))
    completed = parse_ts(info.get("last_completed_automatic_backup"))
    ev = {"attempted": attempted and attempted.isoformat(), "completed": completed and completed.isoformat()}
    if attempted is None:
        return result(cid, WARN, "automatic backups have never run", evidence=ev)
    in_grace = now - attempted <= timedelta(hours=grace_h)
    if completed is None and in_grace:
        return result(cid, PASS, "first automatic backup in progress", evidence=ev)
    if completed is None or (attempted > completed and not in_grace):
        return result(cid, FAIL, "last automatic backup attempt did not complete", evidence=ev)
    return result(cid, PASS, "last automatic backup completed", evidence=ev)


# ---------------------------------------------------------------- repairs / notifications / updates

def check_repairs(issues):
    cid = "ha.repairs"
    if not isinstance(issues, list):
        return unknown(cid, "repairs/list_issues returned no list")
    active = [i for i in issues if not i.get("ignored") and not i.get("dismissed_version")]
    names = [f"{i.get('domain')}:{i.get('issue_id')}" for i in active]
    ev = {"count": len(active), "issues": names[:20]}
    if any(i.get("severity") == "critical" for i in active):
        return result(cid, FAIL, f"{len(active)} repairs, incl. critical", evidence=ev)
    if active:
        return result(cid, WARN, f"{len(active)} open repairs", evidence=ev)
    return result(cid, PASS, "no open repairs", evidence=ev)


def check_notifications(notifications):
    cid = "ha.notifications"
    if isinstance(notifications, dict):
        notifications = list(notifications.values())
    if not isinstance(notifications, list):
        return unknown(cid, "persistent_notification/get returned no list")
    # Count only: titles are free text written by automations and can reveal presence/location.
    ev = {"count": len(notifications)}
    if notifications:
        return result(cid, WARN, f"{len(notifications)} persistent notifications", evidence=ev)
    return result(cid, PASS, "no persistent notifications", evidence=ev)


def integration_domains(config):
    """Integration domains loaded in this HA, from get_config `components`. Entries look like "mqtt" or a platform
    pair such as "sensor.mqtt"; both halves are integrations a breaking change can be labelled with. None = unread."""
    comps = config.get("components") if isinstance(config, dict) else None
    if not isinstance(comps, list):
        return None
    return sorted({part for c in comps if isinstance(c, str) for part in c.split(".") if part})


def check_updates(states, integrations=None):
    """Waiting updates, with versions (central matches them against each release's breaking changes) and the
    integrations this house loads (domains only), so central can say which breaking changes touch this house."""
    cid = "ha.updates"
    if not isinstance(states, list):
        return unknown(cid, "get_states returned no list")
    waiting = [s for s in states if s.get("entity_id", "").startswith("update.") and s.get("state") == "on"]
    # HA's own updates first, so a house with 30+ waiting updates still reports the Core version.
    waiting.sort(key=lambda s: "home_assistant" not in s["entity_id"])
    pending = [s["entity_id"] for s in waiting]
    versions = {s["entity_id"]: {"installed": str(s.get("attributes", {}).get("installed_version") or "")[:40],
                                 "latest": str(s.get("attributes", {}).get("latest_version") or "")[:40]}
                for s in waiting[:30]}
    ev = {"count": len(pending), "pending": pending[:30], "versions": versions}
    if integrations is not None:
        ev["integrations"] = integrations[:400]
    # Waiting updates are information for the operator, not a fault: PASS with detail.
    return result(cid, PASS, f"{len(pending)} updates waiting" if pending else "up to date", evidence=ev)


# ---------------------------------------------------------------- devices

SILENT_PLATFORMS = ("mqtt", "esphome")
BAD = ("unavailable", "unknown")


def _newest(sts, field):
    stamps = [t for t in (parse_ts(s.get(field)) for s in sts) if t]
    return max(stamps) if stamps else None


def _last_seen(sts):
    """Zigbee2MQTT's own last_seen timestamp (sensor.*_last_seen), when that entity is enabled.
    It is published by z2m, so an HA restart does not reset it."""
    for s in sts:
        if s.get("entity_id", "").endswith("_last_seen") and s.get("state") not in BAD:
            try:
                return parse_ts(s.get("state"))
            except ValueError:
                return None
    return None


Z2M_DEVICE_PREFIX = "zigbee2mqtt_0x"  # z2m end device: ("mqtt", "zigbee2mqtt_0x<ieee>")
Z2M_BRIDGE_PREFIX = "zigbee2mqtt_bridge_"  # the bridge itself: ("mqtt", "zigbee2mqtt_bridge_0x<ieee>")


def _mqtt_ids(device):
    """The device's MQTT identifiers. The registry serialises identifiers as [[domain, id], ...]."""
    return [str(i[1]) for i in device.get("identifiers") or []
            if isinstance(i, (list, tuple)) and len(i) == 2 and i[0] == "mqtt"]


def _z2m_devices(device_registry):
    """Ids of Zigbee2MQTT end devices — not the bridge, z2m groups or other MQTT-discovery devices
    (e.g. BirdNET-Go). Identifier prefix first; a device routed via the z2m bridge counts too."""
    bridges = {d.get("id") for d in device_registry
               if any(i.startswith(Z2M_BRIDGE_PREFIX) for i in _mqtt_ids(d))}
    return {d.get("id") for d in device_registry
            if any(i.startswith(Z2M_DEVICE_PREFIX) for i in _mqtt_ids(d))
            or (d.get("via_device_id") and d.get("via_device_id") in bridges)}


def check_silent_devices(states, entity_registry, device_registry, now, silent_h=24, exceptions=(),
                         stale_h=48, memory=None):
    """Devices (z2m + ESPHome) that have stopped reporting.

    HA resets `last_changed` of an unavailable entity on every restart (LL-50), so "unavailable since"
    from HA alone under-counts. Two restart-proof sources are used on top of it:
      * z2m's own `*_last_seen` sensor, when enabled for that device;
      * `memory` — {device_id: iso time first seen silent}, kept by the agent across runs and restarts.
        This function updates `memory` in place (adds newly silent devices, drops recovered ones).
    """
    cid = "devices.silent"
    if not all(isinstance(x, list) for x in (states, entity_registry, device_registry)):
        return unknown(cid, "states / entity registry / device registry not readable")
    memory = {} if memory is None else memory
    by_id = {s["entity_id"]: s for s in states if "entity_id" in s}
    names = {d.get("id"): d.get("name_by_user") or d.get("name") for d in device_registry}
    z2m = _z2m_devices(device_registry)
    devices = {}
    for e in entity_registry:
        if e.get("platform") not in SILENT_PLATFORMS or not e.get("device_id") or e.get("disabled_by"):
            continue
        st = by_id.get(e.get("entity_id"))
        if st is not None:
            devices.setdefault(e["device_id"], []).append(st)
    silent, stale, watching, no_last_seen = [], [], [], []
    for dev_id, sts in devices.items():
        name = names.get(dev_id) or dev_id
        if dev_id in z2m and not any(s["entity_id"].endswith("_last_seen") for s in sts):
            no_last_seen.append(name)
        if name in exceptions:
            memory.pop(dev_id, None)
            continue
        seen = _last_seen(sts)
        if all(s.get("state") in BAD for s in sts):
            candidates = [t for t in (_newest(sts, "last_changed"), parse_ts(memory.get(dev_id)), seen) if t]
            since = min(candidates) if candidates else now
            memory.setdefault(dev_id, since.isoformat())
            if now - since > timedelta(hours=silent_h):
                silent.append((name, hours(now - since)))
            else:
                watching.append((name, since, hours(now - since)))
            continue
        memory.pop(dev_id, None)
        if seen and now - seen > timedelta(hours=silent_h):
            silent.append((name, hours(now - seen)))
            continue
        # Not unavailable, but nothing heard: with z2m availability off (the default) a dead
        # device keeps its last value forever. last_reported moves on every message received.
        heard = seen or _newest(sts, "last_reported") or _newest(sts, "last_updated")
        if heard and now - heard > timedelta(hours=stale_h):
            stale.append((name, hours(now - heard)))
    for dev_id in [d for d in memory if d not in devices]:
        memory.pop(dev_id)
    silent.sort(key=lambda x: -x[1])
    stale.sort(key=lambda x: -x[1])
    watching.sort(key=lambda x: -x[2])
    ev = {"devices_checked": len(devices),
          "silent": [{"name": n, "silent_h": h} for n, h in silent],
          "not_heard": [{"name": n, "h": h} for n, h in stale],
          "watching_since_restart": len(memory),
          "unavailable_under_threshold": [{"name": n, "since": t.isoformat(), "h": h} for n, t, h in watching[:25]],
          "zigbee_without_last_seen": sorted(no_last_seen)[:60]}
    if not devices:
        return unknown(cid, "no z2m/ESPHome devices found — registry format changed?")
    if stale and not silent:
        listed = ", ".join(f"{n} ({round(h / 24, 1)} d)" for n, h in stale[:5])
        return result(cid, WARN, f"{len(stale)} devices not heard from in > {stale_h} h: {listed}", evidence=ev)
    if silent:
        listed = ", ".join(f"{n} ({round(h / 24, 1)} d)" for n, h in silent[:5])
        return result(cid, WARN, f"{len(silent)} devices silent > {silent_h} h: {listed}",
                      f"{len(silent)} device(s) have stopped reporting and may need a battery or a reset.",
                      "household", ev)
    note = f"; {len(memory)} unavailable, under {silent_h} h so far" if memory else ""
    return result(cid, PASS, f"all {len(devices)} devices reporting{note}", evidence=ev)


FLAP_DOMAINS = ("switch", "light", "fan", "lock", "cover", "climate", "media_player", "humidifier", "vacuum")


def flap_candidates(entity_registry, device_registry):
    """One entity per device, from domains whose state changes are rare, so 24 h of history stays small.
    Sensor-only devices are left to devices.silent. -> {entity_id: device name}"""
    if not isinstance(entity_registry, list) or not isinstance(device_registry, list):
        return {}
    names = {d.get("id"): d.get("name_by_user") or d.get("name") for d in device_registry}
    picked = {}
    for e in sorted(entity_registry, key=lambda e: e.get("entity_id", "")):
        dev, eid = e.get("device_id"), e.get("entity_id", "")
        if (not dev or dev in picked or e.get("disabled_by") or e.get("entity_category")
                or eid.split(".", 1)[0] not in FLAP_DOMAINS):
            continue
        picked[dev] = eid
    return {eid: names.get(dev) or eid for dev, eid in picked.items()}


FLAP_MIN_DROP_S = 60


def _row_time(r):
    try:
        return parse_ts(r.get("lc") or r.get("lu"))
    except (ValueError, TypeError, OverflowError):
        return None


def check_flapping(history, candidates, min_drops=3, min_drop_s=FLAP_MIN_DROP_S):
    """Devices that dropped off (-> unavailable) `min_drops` or more times in the window — e.g. a WiFi plug that
    keeps falling off the network. Each drop is short, so hourly snapshots never see it; history does.
    history = {entity_id: [{"s": state, ...}, ...]} (HA's compressed history/history_during_period).
    A drop shorter than `min_drop_s` is not counted: every HA restart or integration reload marks entities
    unavailable for under a second (2026-10-13: 9 of a Zigbee bulb's 13 'drops' in a week were reload blips; its
    real drops lasted 16 min and more). Rows without timestamps are counted as before."""
    cid = "devices.flapping"
    if not candidates:
        return result(cid, PASS, "no switch/light-type devices to watch", evidence={"devices_checked": 0})
    if not isinstance(history, dict):
        return unknown(cid, "history/history_during_period returned no dict")
    flappy = []
    for eid, rows in history.items():
        if eid not in candidates or not isinstance(rows, list):
            continue
        drops, prev = 0, None
        rows = [r for r in rows if isinstance(r, dict)]
        for i, r in enumerate(rows):
            s = r.get("s")
            if s == "unavailable" and prev not in (None, "unavailable"):
                back = next((x for x in rows[i + 1:] if x.get("s") != "unavailable"), None)
                t0, t1 = _row_time(r), _row_time(back) if back else None
                if not (t0 and t1 and (t1 - t0).total_seconds() < min_drop_s):
                    drops += 1                 # long enough, still down, or no timestamps to tell
            prev = s
        if drops >= min_drops:
            flappy.append((candidates[eid], drops))
    flappy.sort(key=lambda x: -x[1])
    ev = {"devices_checked": len(candidates), "flapping": [{"name": n, "drops_24h": d} for n, d in flappy]}
    if not flappy:
        return result(cid, PASS, f"no device dropped off {min_drops}+ times in 24 h", evidence=ev)
    listed = ", ".join(f"{n} ({d}x)" for n, d in flappy[:5])
    return result(cid, WARN, f"{len(flappy)} device(s) keep dropping off: {listed}", evidence=ev)


def _device_heard(states, entity_registry):
    """{device_id: newest last_reported/last_updated over all its entities}. A battery reading only changes when the
    level does, so the device's other entities (temperature etc.) say whether it is still talking."""
    dev = {e.get("entity_id"): e.get("device_id") for e in entity_registry if e.get("device_id")}
    out = {}
    for s in states:
        d = dev.get(s.get("entity_id"))
        t = parse_ts(s.get("last_reported") or s.get("last_updated")) if d else None
        if t and (d not in out or t > out[d]):
            out[d] = t
    return out


def check_batteries(states, entity_registry, warn_pct=25, fail_pct=10, exclude_platforms=("mobile_app",),
                    now=None, stale_h=24):
    cid = "devices.battery"
    if not isinstance(states, list) or not isinstance(entity_registry, list):
        return unknown(cid, "states / entity registry not readable")
    platform = {e.get("entity_id"): e.get("platform") for e in entity_registry}
    device = {e.get("entity_id"): e.get("device_id") for e in entity_registry}
    heard = _device_heard(states, entity_registry) if now else {}
    low, levels, quiet = [], {}, []
    seen = 0
    for s in states:
        a = s.get("attributes", {})
        if a.get("device_class") != "battery" or a.get("unit_of_measurement") != "%":
            continue
        if platform.get(s.get("entity_id")) in exclude_platforms:
            continue
        try:
            pct = float(s.get("state"))
        except (TypeError, ValueError):
            continue
        seen += 1
        name = a.get("friendly_name") or s["entity_id"]
        levels[name[:60]] = round(pct)
        if pct > warn_pct:
            continue
        last = heard.get(device.get(s["entity_id"]))
        if last and now - last > timedelta(hours=stale_h):
            # The device stopped talking: its last battery number is old news, not a reading (2026-10-14: two
            # re-paired sensors that never came back showed "0 %" for a day; the fix was a re-pair, not a battery).
            quiet.append((name, pct, hours(now - last)))
        else:
            low.append((name, pct))
    low.sort(key=lambda x: x[1])
    # `levels` (name -> %, all batteries) lets central project days-left from the trend (Session C).
    ev = {"batteries_checked": seen, "low": [{"name": n, "pct": p} for n, p in low],
          "not_heard": [{"name": n, "last_pct": p, "h": h} for n, p, h in quiet],
          "levels": dict(sorted(levels.items())[:60])}
    if not seen:
        return unknown(cid, "no battery sensors found")
    quiet_txt = ", ".join(f"{n} (not heard for {_gap_text(h)}, last read {p:.0f} %)" for n, p, h in quiet[:5])
    if not low and not quiet:
        return result(cid, PASS, f"all {seen} batteries above {warn_pct} %", evidence=ev)
    if not low:
        return result(cid, WARN, f"{len(quiet)} low reading(s) from devices that stopped reporting: {quiet_txt}",
                      evidence=ev)
    listed = ", ".join(f"{n} {p:.0f} %" for n, p in low[:5])
    if quiet:
        listed += f"; stopped reporting: {quiet_txt}"
    status = FAIL if low[0][1] <= fail_pct else WARN
    first = low[0][0]
    return result(cid, status, f"{len(low)} low: {listed}",
                  f"{first} needs a new battery soon." if len(low) == 1
                  else f"{len(low)} devices need new batteries soon, starting with {first}.",
                  "household", ev)


# ---------------------------------------------------------------- automations

KEEP_TRIGGERS = 30        # trigger times remembered per automation (house-side only)
MIN_TRIGGERS = 5          # need this many (4 gaps) before judging "stopped"


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def check_automations(states, now, memory=None, factor=4, min_silent_h=48, exceptions=()):
    """Automations that stopped firing (the AUTO-11 class: Driveway Alert dead for 7 weeks, nothing noticed) and
    automations HA could not load (state `unavailable`).

    "Stopped" is learned per automation, house-side: `memory` ({entity_id: [iso trigger times]}, kept in
    /data/memory.json) collects each new `last_triggered`; once MIN_TRIGGERS are known, an automation silent for more
    than `factor` x its median gap (and at least `min_silent_h`) is flagged. Rarely-firing automations (leak alarms)
    have long gaps, so they never trip it. Trigger times never leave the house — only the conclusion does.
    Known limit: an automation already dead when the agent is installed never collects MIN_TRIGGERS, so it stays
    "learning" — without history it can't be told apart from a rarely-firing one (shown in evidence as `learning`)."""
    cid = "automations.stopped"
    if not isinstance(states, list):
        return unknown(cid, "get_states returned no list")
    memory = {} if memory is None else memory
    autos = [s for s in states if s.get("entity_id", "").startswith("automation.")]
    if not autos:
        return unknown(cid, "no automation entities found")
    broken, stopped, learning = [], [], 0
    present = set()
    for s in autos:
        eid = s["entity_id"]
        present.add(eid)
        attrs = s.get("attributes") or {}
        name = attrs.get("friendly_name") or eid
        if attrs.get("restored"):
            continue   # placeholder while HA is still starting: not loaded *yet*, not broken
        if name in exceptions or eid in exceptions:
            memory.pop(eid, None)
            continue
        if s.get("state") == "unavailable":
            broken.append(name)
            continue
        if s.get("state") != "on":
            memory.pop(eid, None)          # disabled on purpose: forget, relearn if re-enabled
            continue
        try:
            last = parse_ts((s.get("attributes") or {}).get("last_triggered"))
        except ValueError:
            last = None
        seen = memory.get(eid)
        if not isinstance(seen, list) or not all(isinstance(t, str) and _valid_ts(t) for t in seen):
            seen = []   # a damaged memory entry is relearned, never a check stuck on UNKNOWN
        memory[eid] = seen
        if last and (not seen or last.isoformat() > seen[-1]):
            seen.append(last.isoformat())
            del seen[:-KEEP_TRIGGERS]
        if len(seen) < MIN_TRIGGERS:
            learning += 1
            continue
        times = [parse_ts(t) for t in seen]
        gap_h = _median([(b - a).total_seconds() / 3600 for a, b in pairwise(times)])
        silent_h = (now - times[-1]).total_seconds() / 3600
        if silent_h > max(factor * gap_h, min_silent_h):
            stopped.append((name, silent_h, gap_h))
    for eid in [e for e in memory if e not in present]:
        memory.pop(eid)
    stopped.sort(key=lambda x: -x[1] / max(x[2], 0.01))
    ev = {"automations": len(autos), "learning": learning, "unavailable": sorted(broken)[:30],
          "stopped": [{"name": n, "silent_d": round(s / 24, 1), "usual_gap_h": round(g, 1)} for n, s, g in stopped]}
    bits = []
    if broken:
        bits.append(f"{len(broken)} not loaded (unavailable): {', '.join(sorted(broken)[:5])}")
    if stopped:
        bits.append(f"{len(stopped)} stopped firing: " + ", ".join(
            f"{n} (silent {round(s / 24, 1)} d, usually every {_gap_text(g)})" for n, s, g in stopped[:5]))
    if bits:
        return result(cid, WARN, "; ".join(bits), evidence=ev)
    note = f"; still learning {learning}" if learning else ""
    return result(cid, PASS, f"{len(autos)} automations, none stopped{note}", evidence=ev)


def _valid_ts(text):
    try:
        return parse_ts(text) is not None
    except ValueError:
        return False


def _gap_text(h):
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} d"


# ---------------------------------------------------------------- disk / power

def check_disk(host, warn_free_gb=5.0, fail_free_gb=1.5, warn_pct=85):
    """HA's data disk, from Supervisor /host/info. HA's own repair only appears at ~2 GB free (2026-10-09:
    images of stopped Apps + the ESPHome build cache had filled it). free_gb in evidence lets central forecast
    "full in N days"."""
    cid = "system.disk"
    if not isinstance(host, dict) or not isinstance(host.get("disk_total"), (int, float)) \
            or not isinstance(host.get("disk_free"), (int, float)) or not host["disk_total"]:
        return unknown(cid, "/host/info has no disk_total / disk_free")
    total, free = float(host["disk_total"]), float(host["disk_free"])
    used_pct = round(100 * (total - free) / total, 1)
    ev = {"free_gb": round(free, 2), "total_gb": round(total, 1), "used_pct": used_pct}
    reason = f"data disk {free:.1f} GB free of {total:.0f} GB ({used_pct:.0f} % used)"
    if free < fail_free_gb:
        return result(cid, FAIL, reason, "Home Assistant has almost no disk space left; backups and updates may fail.",
                      "household", ev)
    if free < warn_free_gb or used_pct >= warn_pct:
        return result(cid, WARN, reason, evidence=ev)
    return result(cid, PASS, reason, evidence=ev)


def _power_sensors(states, entity_registry):
    """[(entity_id, watts, last_changed, device_id, unique_id)] — every power sensor (W) that sits on a device."""
    by_id = {s.get("entity_id"): s for s in states}
    out = []
    for e in entity_registry:
        st = by_id.get(e.get("entity_id"))
        if not st or not e.get("device_id") or e.get("disabled_by") or not e["entity_id"].startswith("sensor."):
            continue
        a = st.get("attributes") or {}
        if a.get("device_class") != "power" or a.get("unit_of_measurement") != "W":
            continue
        try:
            watts = float(st.get("state"))
        except (TypeError, ValueError):
            continue
        out.append((e["entity_id"], watts, parse_ts(st.get("last_changed")), e["device_id"], e.get("unique_id")))
    return out


def _uid_meter(uid, meters):
    """The power sensor whose unique_id is `<switch unique_id>_...`, on whatever device: an HS300 puts each outlet's
    switch on the strip's device but that outlet's meter on a child device of its own (2026-10-10: all 18 outlets in
    the house, e.g. 'Kitchen Fan' = <uid> + '_current_power_w'). `_current_power_w` wins, else the shortest."""
    if not isinstance(uid, str) or not uid:
        return None
    own = [m for m in meters if isinstance(m[4], str) and m[4].startswith(uid + "_")]
    if not own:
        return None
    exact = [m for m in own if m[4] == uid + "_current_power_w"]
    return exact[0] if exact else min(own, key=lambda m: len(m[4]))


def _switch_meter(eid, dev_switches, meters):
    """The power sensor that measures this switch, or None. A multi-outlet strip (HS300) can put every outlet's switch
    and meter on one device: the meter whose entity id starts with the switch's own is its one; a single-switch device
    uses its meter. The strip's own switch is skipped (None) when its outlets have switches: it is on whenever one
    outlet is, so it would repeat that outlet's finding (2026-10-10: 'Power Strip_7A93' + 'Power Strip_7A93 Plug 2')."""
    obj = eid.split(".", 1)[1]
    if any(o != eid and o.split(".", 1)[1].startswith(obj + "_") for o in dev_switches):
        return None
    own = [m for m in meters if m[0].split(".", 1)[1].startswith(obj + "_")]
    if own:
        return min(own, key=lambda m: len(m[0]))
    return meters[0] if len(dev_switches) == 1 else None


def check_zero_power(states, entity_registry, now, memory=None, hours_needed=6, max_w=0.5, exceptions=(),
                     found=None):
    """A switch that is ON while its own power meter has read ~0 W for hours: the HVAC-03 class (the gym fan was
    commanded on ~80 min/day for 41 days while unplugged). Kasa meters take a while to register watts, so a plug only
    counts after `hours_needed` of on-and-zero, tracked across runs in `memory` ({switch: iso since}). Evidence names
    the switch and hours only. `found` (a dict, filled in place) gets {switch entity: {"meter", "hours"}} of the
    flagged ones: it stays in the house (the status sensor's `idle_outlets`, for the turn-off self-heal), never sent."""
    cid = "devices.zero_power"
    if not isinstance(states, list) or not isinstance(entity_registry, list):
        return unknown(cid, "states / entity registry not readable")
    memory = {} if memory is None else memory
    all_meters = _power_sensors(states, entity_registry)
    meters = {}
    for m in all_meters:
        meters.setdefault(m[3], []).append(m)
    by_id = {s.get("entity_id"): s for s in states}
    switches = [e for e in entity_registry
                if e.get("entity_id", "").startswith("switch.") and e.get("device_id")
                and not e.get("disabled_by") and not e.get("entity_category") and e["entity_id"] in by_id]
    by_uid = {e["entity_id"]: _uid_meter(e.get("unique_id"), all_meters) for e in switches}
    switches = [e for e in switches if by_uid[e["entity_id"]] or e["device_id"] in meters]
    by_dev = {}
    for e in switches:
        by_dev.setdefault(e["device_id"], []).append(e["entity_id"])

    def child_metered(eid, dev):   # an outlet whose meter sits on its own child device
        return by_uid[eid] is not None and by_uid[eid][3] != dev

    watched, flagged, seen = 0, [], set()
    for e in switches:
        eid, dev = e["entity_id"], e["device_id"]
        meter = by_uid[eid]
        if not child_metered(eid, dev) and any(child_metered(o, dev) for o in by_dev[dev] if o != eid):
            continue   # the strip's main switch, beside outlets that have meters of their own
        if meter is None:
            meter = _switch_meter(eid, by_dev[dev], meters.get(dev, []))
        if meter is None:
            continue
        st = by_id[eid]
        name = (st.get("attributes") or {}).get("friendly_name") or eid
        if name in exceptions or eid in exceptions:
            continue
        watched += 1
        seen.add(eid)
        _, watts, zero_since = meter[:3]
        if st.get("state") != "on" or watts > max_w:
            memory.pop(eid, None)
            continue
        try:
            since = parse_ts(memory.get(eid)) if isinstance(memory.get(eid), str) else None
        except ValueError:
            since = None
        if since is None:   # first sighting: on-and-zero began when the later of the two did
            starts = [t for t in (parse_ts(st.get("last_changed")), zero_since) if t]
            since = max(starts) if starts else now
        memory[eid] = since.isoformat()
        hrs = hours(now - since)
        if hrs >= hours_needed:
            flagged.append((name, hrs))
            if found is not None:
                found[eid] = {"meter": meter[0], "hours": hrs}
    for eid in [k for k in memory if k not in seen]:
        memory.pop(eid)
    flagged.sort(key=lambda x: -x[1])
    ev = {"switches_with_meter": watched, "zero_power": [{"name": n, "hours": h} for n, h in flagged]}
    if not flagged:
        return result(cid, PASS, f"{watched} metered switches, none on at 0 W for {hours_needed}+ h", evidence=ev)
    listed = ", ".join(f"{n} ({_gap_text(h)})" for n, h in flagged[:5])
    return result(cid, WARN, f"{len(flagged)} switched on but drawing nothing: {listed}", evidence=ev)


# ---------------------------------------------------------------- integrations / log

FAILED_STATES = ("setup_error", "setup_retry", "migration_error", "failed_unload")


def check_integrations(entries, memory=None, exceptions=()):
    """Integrations HA could not load (setup_error) or keeps retrying (setup_retry) — e.g. 2026-10-09: the desktop
    plug's integration had been 'failed' for 57 days and only Steve noticed. An entry counts after two hourly runs in a
    row (memory: {entry_id: iso first seen}), so a device rebooting at report time is not news. Ignored and disabled
    entries are skipped. Leaves the house: integration name (domain) and its title, e.g. "wled: Entertainment Stand"."""
    cid = "ha.integrations"
    if not isinstance(entries, list):
        return unknown(cid, "config_entries/get returned no list")
    memory = {} if memory is None else memory
    bad, now_failing = [], set()
    active = [e for e in entries if isinstance(e, dict) and e.get("source") != "ignore" and not e.get("disabled_by")]
    for e in active:
        label = f"{e.get('domain', '?')}: {e.get('title') or '?'}"
        if label in exceptions or e.get("domain") in exceptions or e.get("title") in exceptions:
            continue
        if e.get("state") in FAILED_STATES:
            eid = e.get("entry_id") or label
            now_failing.add(eid)
            if eid in memory:
                bad.append((label, e.get("state"), memory[eid]))
            else:
                memory[eid] = "seen"
    for eid in [k for k in memory if k not in now_failing]:
        memory.pop(eid)
    ev = {"integrations": len(active), "failing": [{"name": n, "state": s} for n, s, _ in bad]}
    if not bad:
        waiting = len(now_failing)    # first sighting only: say so instead of "all loaded" (it isn't)
        if waiting:
            return result(cid, PASS, f"{len(active) - waiting} of {len(active)} integrations loaded; {waiting} not "
                          "loading, checked again next run", evidence=ev)
        return result(cid, PASS, f"all {len(active)} integrations loaded", evidence=ev)
    listed = ", ".join(f"{n} ({s.replace('_', ' ')})" for n, s, _ in sorted(bad)[:6])
    return result(cid, WARN, f"{len(bad)} integration(s) not working: {listed}", evidence=ev)


def _log_integration(name):
    """Logger name -> integration: homeassistant.components.<x>[...] / custom_components.<x>[...]; else the logger."""
    parts = (name or "?").split(".")
    for i, p in enumerate(parts[:-1]):
        if p in ("components", "custom_components"):
            return parts[i + 1]
    return parts[0] if parts[0] != "homeassistant" else ".".join(parts[:2])


_LOG_SUBJECT = re.compile(r"(?:Error fetching|Failed to update device at|connecting to the device:?) "
                          r"([A-Za-z0-9 ._-]{1,40}?)(?: data|:| |$)")
_PRIVATE_IP = re.compile(r"^(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.[\d.]+$")


def _log_subjects(messages):
    """Which device a coordinator error is about, from HA's 'Error fetching <name or IP> data' lines: a device name or
    a private LAN address, never more of the message. (2026-10-14: 'tplink 122x' was two plugs.)"""
    out = []
    for m in messages or []:
        hit = _LOG_SUBJECT.search(str(m))
        if not hit:
            continue
        subj = hit.group(1).strip()
        if "." in subj and not _PRIVATE_IP.match(subj):
            continue
        short = "." + subj.rsplit(".", 1)[-1] if _PRIVATE_IP.match(subj) else subj
        if short not in out:
            out.append(short)
    return out


def check_log_errors(entries, now, threshold=10, window_h=24):
    """Errors in HA's log over the last day, by integration — 2026-10-07 the log held ~1,500 errors nobody had seen,
    and the dead Driveway Alert surfaced only there. Reads system_log/list (HA's own deduplicated error list).
    Leaves the house: integration names, counts, and which device a fetch error is about (its name or the last part of
    its LAN address, e.g. '.117'), never the log text."""
    cid = "ha.log_errors"
    if not isinstance(entries, list):
        return unknown(cid, "system_log/list returned no list")
    since = now - timedelta(hours=window_h)
    per, subjects = {}, {}
    for e in entries:
        if not isinstance(e, dict) or e.get("level") not in ("ERROR", "CRITICAL"):
            continue
        try:
            last = parse_ts(e.get("timestamp"))
        except (ValueError, TypeError, OverflowError):
            last = None
        if last is None or last < since:
            continue
        name = _log_integration(e.get("name"))
        per[name] = per.get(name, 0) + int(e.get("count") or 1)
        for s in _log_subjects(e.get("message")):
            if s not in subjects.setdefault(name, []):
                subjects[name].append(s)
    ranked = sorted(per.items(), key=lambda x: -x[1])
    total = sum(per.values())
    ev = {"errors_24h": total, "by_integration": dict(ranked[:15]),
          "devices": {n: s[:5] for n, s in subjects.items() if s}}
    noisy = [(n, c) for n, c in ranked if c >= threshold]
    if noisy:
        listed = ", ".join(f"{n} {c}x" + (f" (from {', '.join(subjects[n][:4])})" if subjects.get(n) else "")
                           for n, c in noisy[:6])
        return result(cid, WARN, f"{total} errors in HA's log in {window_h} h: {listed}", evidence=ev)
    if total:
        return result(cid, PASS, f"{total} error(s) in HA's log in {window_h} h, none repeating {threshold}+ times",
                      evidence=ev)
    return result(cid, PASS, f"no errors in HA's log in {window_h} h", evidence=ev)


# ---------------------------------------------------------------- platform / site-specific

def check_supervisor(resolution):
    cid = "supervisor.health"
    if not isinstance(resolution, dict) or "unhealthy" not in resolution:
        return unknown(cid, "/resolution/info has no 'unhealthy'")
    unhealthy, unsupported = resolution.get("unhealthy") or [], resolution.get("unsupported") or []
    ev = {"unhealthy": unhealthy, "unsupported": unsupported,
          "issues": [i.get("type") for i in resolution.get("issues") or []]}
    if unhealthy:
        return result(cid, FAIL, f"Supervisor unhealthy: {', '.join(unhealthy)}", evidence=ev)
    if unsupported:
        return result(cid, WARN, f"unsupported: {', '.join(unsupported)}", evidence=ev)
    return result(cid, PASS, "Supervisor healthy", evidence=ev)


def check_watched_entity(states, rule, now):
    """Site-specific: an entity must not sit in a bad state longer than `for_min`.
    rule = {"entity": "sensor.alarm_status", "bad": ["Fault"], "for_min": 60}"""
    eid = rule.get("entity", "?")
    cid = f"watch.{eid}"
    if not isinstance(states, list):
        return unknown(cid, "get_states returned no list")
    st = next((s for s in states if s.get("entity_id") == eid), None)
    if st is None:
        return unknown(cid, f"{eid} does not exist")
    since = parse_ts(st.get("last_changed"))
    ev = {"state": st.get("state"), "since": since and since.isoformat()}
    if st.get("state") in rule.get("bad", []):
        mins = (now - since).total_seconds() / 60 if since else 0
        if mins >= rule.get("for_min", 60):
            return result(cid, FAIL, f"{eid} = {st.get('state')} for {round(mins)} min", evidence=ev)
        return result(cid, WARN, f"{eid} = {st.get('state')} ({round(mins)} min)", evidence=ev)
    return result(cid, PASS, f"{eid} = {st.get('state')}", evidence=ev)


# House facts (DEC-48): numbers the house computes about itself (heating per degree-day, the downstairs gap, each
# against the house's own normal). Only these whitelisted numeric fields leave the house; central compares and words.
FACT_FIELDS = {
    "heating": ("min_per_hdd_7d", "days_7d", "min_per_hdd_normal", "normal_days", "outdoor_mean_7d"),
    "downstairs_gap": ("mean_7d", "normal", "normal_days"),
}
FACTS_STALE_DAYS = 3


def check_house_facts(states, eid, now):
    """Opt-in (`facts_entity`): read the facts entity's attributes. Missing entity -> UNKNOWN (never PASS);
    facts older than FACTS_STALE_DAYS -> WARN (operator: the house stopped computing them)."""
    cid = "house.facts"
    if not isinstance(states, list):
        return unknown(cid, "get_states returned no list")
    st = next((s for s in states if s.get("entity_id") == eid), None)
    if st is None:
        return unknown(cid, f"{eid} does not exist")
    attrs = st.get("attributes") or {}
    ev = {"through": str(st.get("state"))[:10]}
    for group, fields in FACT_FIELDS.items():
        src = attrs.get(group) if isinstance(attrs.get(group), dict) else {}
        vals = {f: src[f] for f in fields if isinstance(src.get(f), (int, float)) and not isinstance(src.get(f), bool)}
        if vals:
            ev[group] = vals
    if len(ev) == 1:
        return unknown(cid, f"{eid} has no usable facts yet")
    try:
        through = parse_ts(ev["through"] + "T00:00:00+00:00")
    except ValueError:
        through = None
    if through and (now - through).days > FACTS_STALE_DAYS:
        return result(cid, WARN, f"house facts not updated since {ev['through']}", evidence=ev)
    return result(cid, PASS, f"house facts through {ev['through']}", evidence=ev)


# ---------------------------------------------------------------- alarm events (DEC-10 processes)
# Watson looks once an hour, so an alarm that fires and clears in between never shows in a snapshot (2026-10-12: a
# 42 s HVAC Critical at 20:15, HVAC Fault <-> Normal three times 14:11-15:33). This check reads what happened instead:
#   * states: `history/history_during_period` of every entity labelled `alarm_process` (the same set that rolls up
#     into sensor.estate_alarm_status) -> each non-Normal episode with start, end, duration and worst tier;
#   * words: the logbook lines script.estate_alarm_notify writes for every annunciation ("Alarm: <Process>",
#     "<TIER> - <message>") -> the alarm's own message text. They are also a second witness: HA's history API has
#     been seen to drop rows (LL-24), so an annunciation with no matching state change still becomes an episode
#     (marked src "logbook").
# The logbook is read incrementally (since the last good read, kept in /data/memory.json) so each run asks HA for
# about an hour of it, not a whole day. Facts only (DEC-34): central decides what is worth telling anyone.

ALARM_LABEL = "alarm_process"
ALARM_TIERS = ("Warn", "Fault", "Critical", "Tripped")       # rising severity, as sensor.estate_alarm_status ranks
LOG_TIERS = {"WARN": "Warn", "FAULT": "Fault", "CRITICAL": "Critical", "TRIP": "Tripped", "CLEAR": "Normal"}
ALARM_WINDOW_H = 24
ALARM_MAX_EPISODES = 80        # per card; the per-process totals always cover every episode
ALARM_MSG_CHARS = 160
ALARM_LOG_KEEP = 300           # logbook lines remembered house-side
ALARM_MATCH_S = 120            # a logbook line belongs to an episode if it is within this of its start/end


def alarm_process_key(name):
    """The id script.estate_alarm_notify derives from a process name: 'Sauna Heat' -> 'sauna_heat'."""
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


MAINTENANCE_LABEL = "maintenance"   # HA label "Maintenance": known, being worked on, don't report it
KEEP_ON_LABEL = "keep_on"           # HA label "Keep on": an outlet that may sit on at 0 W (never flagged, never cut)
RARELY_USED_LABEL = "rarely_used"   # HA label "Rarely used": an automation that may go quiet for weeks (guest room)


def labelled(entity_registry, device_registry, label):
    """Entity ids carrying `label`, on the entity itself or on its device (one label on a device covers all of it)."""
    if not isinstance(entity_registry, list):
        return set()
    devs = {d.get("id") for d in device_registry or [] if isinstance(d, dict) and label in (d.get("labels") or [])}
    return {e["entity_id"] for e in entity_registry
            if isinstance(e, dict) and isinstance(e.get("entity_id"), str)
            and (label in (e.get("labels") or []) or (e.get("device_id") and e.get("device_id") in devs))}


def check_maintenance(entity_registry, device_registry, states, now, memory=None, max_days=14):
    """What the household has parked with the Maintenance label. Parked things are left out of the device and
    automation checks, so this check keeps them from being forgotten: listed every time, WARN once any has been
    parked longer than `max_days` (like an exception that runs out). `memory` = {key: iso first seen}."""
    cid = "devices.maintenance"
    if not isinstance(entity_registry, list) or not isinstance(device_registry, list):
        return unknown(cid, "entity / device registry not readable")
    memory = {} if memory is None else memory
    names = {}
    for d in device_registry:
        if isinstance(d, dict) and MAINTENANCE_LABEL in (d.get("labels") or []):
            names[f"device:{d.get('id')}"] = d.get("name_by_user") or d.get("name") or d.get("id")
    friendly = {s.get("entity_id"): (s.get("attributes") or {}).get("friendly_name") for s in states or []}
    for e in entity_registry:
        if isinstance(e, dict) and MAINTENANCE_LABEL in (e.get("labels") or []):
            names[f"entity:{e.get('entity_id')}"] = friendly.get(e.get("entity_id")) or e.get("entity_id")
    for k in [k for k in memory if k not in names]:
        memory.pop(k)
    ages = {}
    for k in names:
        try:
            since = parse_ts(memory.get(k)) if isinstance(memory.get(k), str) else None
        except ValueError:
            since = None
        since = since or now
        memory[k] = since.isoformat()
        ages[k] = (now - since).total_seconds() / 86400
    ev = {"parked": [{"name": names[k], "days": round(ages[k], 1)} for k in sorted(names, key=lambda k: -ages[k])]}
    if not names:
        return result(cid, PASS, "nothing in maintenance", evidence=ev)
    over = sorted((k for k in names if ages[k] > max_days), key=lambda k: -ages[k])
    if over:
        listed = ", ".join(f"{names[k]} ({ages[k]:.0f} d)" for k in over[:5])
        return result(cid, WARN, f"{len(over)} in maintenance over {max_days} d: {listed}; fix or take the label off",
                      f"{len(over)} thing(s) have been in maintenance for over {max_days} days.", "household", ev)
    listed = ", ".join(names[k] for k in list(names)[:8])
    return result(cid, PASS, f"{len(names)} in maintenance (not checked): {listed}", evidence=ev)


def alarm_entities(entity_registry):
    """{entity_id: process key} for entities carrying the `alarm_process` label; the key is the object id without
    `_status` (input_select.sauna_heat_status -> sauna_heat), which is what the notify script makes of 'Sauna Heat'.
    None when the registry is unreadable or has no `labels` field at all (a format change, not "no alarms")."""
    if not isinstance(entity_registry, list):
        return None
    regs = [e for e in entity_registry if isinstance(e, dict)]
    if regs and not any("labels" in e for e in regs):
        return None
    out = {}
    for e in regs:
        labels = e.get("labels") if isinstance(e.get("labels"), list) else []
        if ALARM_LABEL in labels and not e.get("disabled_by") and isinstance(e.get("entity_id"), str):
            out[e["entity_id"]] = e["entity_id"].split(".", 1)[-1].removesuffix("_status")
    return out


def alarm_log_since(memory, now):
    """Where the next logbook read starts: just before the last good read, never more than the window back."""
    floor = now - timedelta(hours=ALARM_WINDOW_H)
    try:
        last = parse_ts(((memory or {}).get("alarm_log") or {}).get("to"))
    except (ValueError, TypeError, AttributeError):
        last = None
    return max(floor, last - timedelta(minutes=5)) if last else floor


def alarm_log_entries(logbook, keys):
    """'Alarm: <Process>' logbook lines for labelled processes -> [[iso, key, tier, text]]. Other logbook lines
    (and alarm-style lines of processes that don't roll up, e.g. air-quality advisories) are ignored."""
    out = []
    for e in logbook if isinstance(logbook, list) else []:
        if not isinstance(e, dict) or not str(e.get("name") or "").startswith("Alarm: "):
            continue
        key = alarm_process_key(str(e["name"])[len("Alarm: "):])
        word, _, text = str(e.get("message") or "").partition(" - ")
        tier = LOG_TIERS.get(word.strip().upper())
        try:
            when = parse_ts(e.get("when"))
        except (ValueError, TypeError, OverflowError):
            when = None
        if key in keys and tier and when:
            out.append([when.isoformat(), key, tier, text.strip()[:ALARM_MSG_CHARS]])
    return out


def remember_alarm_log(memory, new, read_to, now):
    """Merge newly read lines into memory (deduplicated, last window + 1 h, capped). `read_to` moves on only after a
    good read, so a failed read is retried from the same point next run. Damaged memory is dropped, never fatal."""
    cutoff = now - timedelta(hours=ALARM_WINDOW_H + 1)
    kept = {}
    for x in [*(memory.get("entries") if isinstance(memory.get("entries"), list) else []), *(new or [])]:
        if not (isinstance(x, list) and len(x) == 4 and all(isinstance(v, str) for v in x)):
            continue
        try:
            when = parse_ts(x[0])
        except ValueError:
            continue
        if when and when >= cutoff:
            kept[(when.isoformat(), x[1], x[2])] = [when.isoformat(), x[1], x[2], x[3]]
    entries = [kept[k] for k in sorted(kept)][-ALARM_LOG_KEEP:]
    memory["entries"] = entries
    if read_to:
        memory["to"] = read_to
    return entries


def _state_points(rows):
    """History rows ({"s", "lu"/"lc"}) -> sorted [(time, state)]. Compressed rows give last_changed as `lc` only when
    it differs from `lu`; minimal rows carry the change time in `lu`."""
    pts = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        try:
            t = parse_ts(r.get("lc") or r.get("lu"))
        except (ValueError, TypeError, OverflowError):
            t = None
        if t:
            pts.append((t, r.get("s")))
    return sorted(pts, key=lambda p: p[0])


def _history_episodes(rows, now):
    """Contiguous non-Normal runs. Tier changes inside a run (Fault -> Critical -> Warn) stay one episode whose tier is
    the worst; seconds per tier are kept. `unknown` / `unavailable` (HA restarting, a reload) neither start nor end
    an episode. `Maintenance` (a person parked the process, DEC-62) ends one like Normal: the alarm is deliberately
    off, so a Fault before it is over (2026-10-14: the parked sauna read "Fault ... still on" for 18 h)."""
    eps, cur = [], None
    for t, s in _state_points(rows):
        if s in ("Normal", "Maintenance"):
            if cur:
                cur["tiers"][cur["at"]] = cur["tiers"].get(cur["at"], 0) + (t - cur["since"]).total_seconds()
                cur["end"] = t
                eps.append(cur)
                cur = None
        elif s in ALARM_TIERS:
            if cur is None:
                cur = {"start": t, "end": None, "at": s, "since": t, "tiers": {}}
            elif s != cur["at"]:
                cur["tiers"][cur["at"]] = cur["tiers"].get(cur["at"], 0) + (t - cur["since"]).total_seconds()
                cur["at"], cur["since"] = s, t
    if cur:
        cur["tiers"][cur["at"]] = cur["tiers"].get(cur["at"], 0) + (now - cur["since"]).total_seconds()
        eps.append(cur)
    return eps


def _worst(tiers):
    return max(tiers, key=ALARM_TIERS.index)


def _covered(when, eps, now):
    pad = timedelta(seconds=ALARM_MATCH_S)
    return any(e["start"] - pad <= when <= (e["end"] or now) + pad for e in eps)


def _logbook_episodes(lines, hist, now):
    """Annunciations with no matching state change in history (LL-24: history can drop rows). Consecutive lines until
    a CLEAR are one episode (Criticals re-push every 15 min); with no CLEAR the end is unknown (None, not 'active')."""
    eps, cur = [], None
    for when, tier, _ in lines:
        if tier == "Normal":
            if cur:
                cur["end"] = when
                eps.append(cur)
                cur = None
            continue
        if _covered(when, hist, now):
            continue
        if cur is None:
            cur = {"start": when, "end": None, "tiers": {}, "src": "logbook"}
        cur["tiers"][tier] = 0
    if cur:
        eps.append(cur)
    return eps


def _message(ep, lines, now):
    """The alarm's own words for an episode: the first logbook line of its worst tier inside it, else the first."""
    pad = timedelta(seconds=ALARM_MATCH_S)
    inside = [(t, x) for w, t, x in lines if t != "Normal" and ep["start"] - pad <= w <= (ep["end"] or now)]
    best = [x for t, x in inside if t == ep["tier"]] or [x for _, x in inside]
    return best[0] if best else None


def _alarm_episode_out(e, now, window_start):
    timed = e["end"] is not None or e["src"] == "history"
    return {"process": e["process"], "tier": e["tier"], "start": e["start"].isoformat(),
            "end": e["end"] and e["end"].isoformat(),
            "s": round(((e["end"] or now) - e["start"]).total_seconds()) if timed else None,
            "tiers": {t: round(v) for t, v in e["tiers"].items()} if e["src"] == "history" else {},
            "msg": e["msg"], "src": e["src"], "before_window": e["start"] < window_start}


def check_alarm_events(states, entities, history, logbook, now, memory=None, share_messages=True):
    """Alarm episodes over the last ALARM_WINDOW_H: per process tier, start, end (None = still on), seconds and the
    alarm's message. PASS whenever readable: the current alarm state has its own check (watch); this is the record of
    what happened, and central decides what is worth mentioning. evidence["v"] versions the shape for central.
    `logbook` = {"events": [...], "to": iso end of the read} or None when the read failed (messages missing, the
    states are still reported). share_messages=False (option share_alarm_messages) keeps the alarm text at home:
    process, tier and timings are still sent, msg is None and evidence["messages"] = "withheld"."""
    cid = "alarms.events"
    memory = {} if memory is None else memory
    if entities is None:
        return unknown(cid, "entity registry unreadable or has no labels")
    window_start = now - timedelta(hours=ALARM_WINDOW_H)
    ev = {"v": 1, "window_h": ALARM_WINDOW_H, "from": window_start.isoformat(), "to": now.isoformat(),
          "processes": {}, "episodes": []}
    if not entities:
        return result(cid, PASS, f"no entities labelled {ALARM_LABEL}", evidence=ev)
    if not isinstance(history, dict):
        return unknown(cid, "history/history_during_period returned no dict")
    read_ok = isinstance(logbook, dict) and isinstance(logbook.get("events"), list)
    new = alarm_log_entries(logbook["events"], set(entities.values())) if read_ok else []
    remembered = remember_alarm_log(memory, new, logbook.get("to") if read_ok else None, now)
    names = {}
    for s in states if isinstance(states, list) else []:
        if s.get("entity_id") in entities:
            fn = str((s.get("attributes") or {}).get("friendly_name") or "")
            names[entities[s["entity_id"]]] = fn.removesuffix(" Status").removesuffix(" status")[:40]
    all_eps = []
    for eid, key in sorted(entities.items()):
        lines = [(parse_ts(w), t, x) for w, k, t, x in remembered if k == key]
        every = [dict(e, src="history") for e in _history_episodes(history.get(eid), now)]
        hist = [e for e in every if (e["end"] or now) >= window_start]
        eps = hist + [e for e in _logbook_episodes(lines, every, now) if e["start"] >= window_start]
        name = names.get(key) or key.replace("_", " ").title()
        for e in eps:
            e["tier"], e["process"] = _worst(e["tiers"]), name
            e["msg"] = _message(e, lines, now) if share_messages else None
        all_eps += eps
        if eps:
            timed = [e for e in eps if e["src"] == "history"]
            active = [e["tier"] for e in timed if e["end"] is None]
            ev["processes"][name] = {
                "count": len(eps), "worst": _worst([e["tier"] for e in eps]),
                "not_normal_s": round(sum(((e["end"] or now) - max(e["start"], window_start)).total_seconds()
                                          for e in timed)),
                "active": active[0] if active else None}
    # More than fit: keep the most severe, then the most recent; sent oldest first.
    picked = sorted(all_eps, key=lambda e: (-ALARM_TIERS.index(e["tier"]), -e["start"].timestamp()))
    picked = sorted(picked[:ALARM_MAX_EPISODES], key=lambda e: e["start"])
    ev["episodes"] = [_alarm_episode_out(e, now, window_start) for e in picked]
    ev["truncated"] = len(all_eps) - len(picked)
    ev["messages"] = ("read" if read_ok else "unread") if share_messages else "withheld"
    note = "" if read_ok else "; alarm messages not read this run"
    if not all_eps:
        return result(cid, PASS, f"no alarms in {ALARM_WINDOW_H} h ({len(entities)} processes watched){note}",
                      evidence=ev)
    ranked = sorted(ev["processes"].items(), key=lambda kv: -ALARM_TIERS.index(kv[1]["worst"]))
    listed = ", ".join(f"{n} {p['count']}x (worst {p['worst']}{', on now' if p['active'] else ''})" for n, p in ranked)
    return result(cid, PASS, f"{len(all_eps)} alarm episode(s) in {ALARM_WINDOW_H} h: {listed}{note}"[:500],
                  evidence=ev)


# ---------------------------------------------------------------- self-heals (blueprints/automation/housekeeper)
#
# The self-heal blueprints run in the house's own Home Assistant (installed by the owner; the App never acts, DEC-34).
# Each attempt writes ONE logbook line:  name "Watson self-heal",  message "<target>: <result> - <what it did>",
# result "fixed" or "still down". This check collects those lines (incrementally, like the alarm lines) so central
# can say "Watson fixed X at 03:12" instead of asking, and "Watson already tried" when it did not work.

SELFHEAL_NAME = "Watson self-heal"
SELFHEAL_RESULTS = ("fixed", "still down")
SELFHEAL_KEEP = 200


def selfheal_log_entries(logbook):
    """'Watson self-heal' logbook lines -> [[iso, target, result, detail]]. Malformed lines are skipped."""
    out = []
    for e in logbook if isinstance(logbook, list) else []:
        if not isinstance(e, dict) or str(e.get("name") or "") != SELFHEAL_NAME:
            continue
        target, sep, rest = str(e.get("message") or "").partition(": ")
        res, _, detail = rest.partition(" - ")
        res = res.strip().lower()
        try:
            when = parse_ts(e.get("when"))
        except (ValueError, TypeError, OverflowError):
            when = None
        if sep and when and target.strip() and res in SELFHEAL_RESULTS:
            out.append([when.isoformat(), target.strip()[:80], res, detail.strip()[:120]])
    return out


def check_selfheal_events(logbook, now, memory=None):
    """What the house's self-heal blueprints did in the last 24 h. PASS when everything they tried worked; WARN when
    one could not fix what it tried (the device then also shows in its own check - this says a fix was tried).
    `logbook` = {"events": [...], "to": iso} or None (read failed: lines missing this run, memory still counts)."""
    cid = "selfheal.events"
    memory = {} if memory is None else memory
    read_ok = isinstance(logbook, dict) and isinstance(logbook.get("events"), list)
    new = selfheal_log_entries(logbook["events"]) if read_ok else []
    cutoff = now - timedelta(hours=ALARM_WINDOW_H)
    kept = {}
    for x in [*(memory.get("entries") if isinstance(memory.get("entries"), list) else []), *new]:
        if not (isinstance(x, list) and len(x) == 4 and all(isinstance(v, str) for v in x)):
            continue
        try:
            when = parse_ts(x[0])
        except ValueError:
            continue
        if when and when >= cutoff:
            kept[(when.isoformat(), x[1])] = [when.isoformat(), x[1], x[2], x[3]]
    entries = [kept[k] for k in sorted(kept)][-SELFHEAL_KEEP:]
    memory["entries"] = entries
    if read_ok and logbook.get("to"):
        memory["to"] = logbook["to"]
    heals = [{"when": w, "target": t, "result": r, "detail": d} for w, t, r, d in entries]
    ev = {"heals": heals, "read": "read" if read_ok else "unread"}
    note = "" if read_ok else "; logbook not read this run"
    if not heals:
        return result(cid, PASS, f"no self-heals in {ALARM_WINDOW_H} h{note}", evidence=ev)
    failed = [h for h in heals if h["result"] != "fixed"]
    fixed = [h for h in heals if h["result"] == "fixed"]
    listed = ", ".join(sorted({h["target"] for h in fixed}))
    if failed:
        tried = ", ".join(sorted({h["target"] for h in failed}))
        return result(cid, WARN, f"self-heal tried and could not fix: {tried}"
                      + (f"; fixed: {listed}" if listed else "") + note, evidence=ev)
    return result(cid, PASS, f"Watson fixed {len(fixed)} in {ALARM_WINDOW_H} h: {listed}{note}"[:500], evidence=ev)
