"""Housekeeper checks — pure functions over data the agent has already read.

Each check returns a dict: {id, status, reason, household, audience, evidence}.
status is PASS / WARN / FAIL / UNKNOWN.

Rule (DEC-34): a check that cannot read or understand its source returns UNKNOWN,
never PASS. A breaking HA change must look like a grey check, not a healthy house.
"""
from __future__ import annotations

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


def check_flapping(history, candidates, min_drops=3):
    """Devices that dropped off (-> unavailable) `min_drops` or more times in the window — e.g. a WiFi plug that
    keeps falling off the network. Each drop is short, so hourly snapshots never see it; history does.
    history = {entity_id: [{"s": state, ...}, ...]} (HA's compressed history/history_during_period)."""
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
        for r in rows:
            s = r.get("s") if isinstance(r, dict) else None
            if s == "unavailable" and prev not in (None, "unavailable"):
                drops += 1
            prev = s
        if drops >= min_drops:
            flappy.append((candidates[eid], drops))
    flappy.sort(key=lambda x: -x[1])
    ev = {"devices_checked": len(candidates), "flapping": [{"name": n, "drops_24h": d} for n, d in flappy]}
    if not flappy:
        return result(cid, PASS, f"no device dropped off {min_drops}+ times in 24 h", evidence=ev)
    listed = ", ".join(f"{n} ({d}x)" for n, d in flappy[:5])
    return result(cid, WARN, f"{len(flappy)} device(s) keep dropping off: {listed}", evidence=ev)


def check_batteries(states, entity_registry, warn_pct=25, fail_pct=10, exclude_platforms=("mobile_app",)):
    cid = "devices.battery"
    if not isinstance(states, list) or not isinstance(entity_registry, list):
        return unknown(cid, "states / entity registry not readable")
    platform = {e.get("entity_id"): e.get("platform") for e in entity_registry}
    low, levels = [], {}
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
        if pct <= warn_pct:
            low.append((name, pct))
    low.sort(key=lambda x: x[1])
    # `levels` (name -> %, all batteries) lets central project days-left from the trend (Session C).
    ev = {"batteries_checked": seen, "low": [{"name": n, "pct": p} for n, p in low],
          "levels": dict(sorted(levels.items())[:60])}
    if not seen:
        return unknown(cid, "no battery sensors found")
    if not low:
        return result(cid, PASS, f"all {seen} batteries above {warn_pct} %", evidence=ev)
    listed = ", ".join(f"{n} {p:.0f} %" for n, p in low[:5])
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
    """{device_id: (entity_id, watts, last_changed)} — the device's power sensor (W), if it has one."""
    by_id = {s.get("entity_id"): s for s in states}
    out = {}
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
        out.setdefault(e["device_id"], (e["entity_id"], watts, parse_ts(st.get("last_changed"))))
    return out


def check_zero_power(states, entity_registry, now, memory=None, hours_needed=6, max_w=0.5, exceptions=()):
    """A switch that is ON while its own power meter has read ~0 W for hours: the HVAC-03 class (the gym fan was
    commanded on ~80 min/day for 41 days while unplugged). Kasa meters take a while to register watts, so a plug only
    counts after `hours_needed` of on-and-zero, tracked across runs in `memory` ({switch: iso since}). Evidence names
    the switch and hours only."""
    cid = "devices.zero_power"
    if not isinstance(states, list) or not isinstance(entity_registry, list):
        return unknown(cid, "states / entity registry not readable")
    memory = {} if memory is None else memory
    meters = _power_sensors(states, entity_registry)
    by_id = {s.get("entity_id"): s for s in states}
    watched, flagged, seen = 0, [], set()
    for e in entity_registry:
        eid, dev = e.get("entity_id", ""), e.get("device_id")
        if not eid.startswith("switch.") or dev not in meters or e.get("disabled_by") or e.get("entity_category"):
            continue
        st = by_id.get(eid)
        if not st:
            continue
        name = (st.get("attributes") or {}).get("friendly_name") or eid
        if name in exceptions or eid in exceptions:
            continue
        watched += 1
        seen.add(eid)
        _, watts, zero_since = meters[dev]
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


def check_log_errors(entries, now, threshold=10, window_h=24):
    """Errors in HA's log over the last day, by integration — 2026-10-07 the log held ~1,500 errors nobody had seen,
    and the dead Driveway Alert surfaced only there. Reads system_log/list (HA's own deduplicated error list).
    Leaves the house: integration names and counts only, never the log text."""
    cid = "ha.log_errors"
    if not isinstance(entries, list):
        return unknown(cid, "system_log/list returned no list")
    since = now - timedelta(hours=window_h)
    per = {}
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
    ranked = sorted(per.items(), key=lambda x: -x[1])
    total = sum(per.values())
    ev = {"errors_24h": total, "by_integration": dict(ranked[:15])}
    noisy = [(n, c) for n, c in ranked if c >= threshold]
    if noisy:
        listed = ", ".join(f"{n} {c}x" for n, c in noisy[:6])
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
