"""Housekeeper checks — pure functions over data the agent has already read.

Each check returns a dict: {id, status, reason, household, audience, evidence}.
status is PASS / WARN / FAIL / UNKNOWN.

Rule (DEC-34): a check that cannot read or understand its source returns UNKNOWN,
never PASS. A breaking HA change must look like a grey check, not a healthy house.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def check_updates(states):
    cid = "ha.updates"
    if not isinstance(states, list):
        return unknown(cid, "get_states returned no list")
    pending = [s["entity_id"] for s in states
               if s.get("entity_id", "").startswith("update.") and s.get("state") == "on"]
    ev = {"count": len(pending), "pending": pending[:30]}
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
    devices, platform = {}, {}
    for e in entity_registry:
        if e.get("platform") not in SILENT_PLATFORMS or not e.get("device_id") or e.get("disabled_by"):
            continue
        st = by_id.get(e.get("entity_id"))
        if st is not None:
            devices.setdefault(e["device_id"], []).append(st)
            platform[e["device_id"]] = e.get("platform")
    silent, stale, no_last_seen = [], [], []
    for dev_id, sts in devices.items():
        name = names.get(dev_id) or dev_id
        if platform[dev_id] == "mqtt" and not any(s["entity_id"].endswith("_last_seen") for s in sts):
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
    ev = {"devices_checked": len(devices),
          "silent": [{"name": n, "silent_h": h} for n, h in silent],
          "not_heard": [{"name": n, "h": h} for n, h in stale],
          "watching_since_restart": len(memory),
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


def check_batteries(states, entity_registry, warn_pct=25, fail_pct=10, exclude_platforms=("mobile_app",)):
    cid = "devices.battery"
    if not isinstance(states, list) or not isinstance(entity_registry, list):
        return unknown(cid, "states / entity registry not readable")
    platform = {e.get("entity_id"): e.get("platform") for e in entity_registry}
    low = []
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
        if pct <= warn_pct:
            low.append((a.get("friendly_name") or s["entity_id"], pct))
    low.sort(key=lambda x: x[1])
    ev = {"batteries_checked": seen, "low": [{"name": n, "pct": p} for n, p in low]}
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
