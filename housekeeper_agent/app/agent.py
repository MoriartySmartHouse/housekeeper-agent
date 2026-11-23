"""Housekeeper Agent — runs inside an HA App, hourly.

read (Supervisor + Core via the Supervisor proxy) -> checks -> report card -> POST to central
-> write the household status back into this house's HA as sensor.housekeeper_status.

Read-only towards HA except that one sensor it owns (enforced by tests/test_write_guard.py).
Every read goes through `fetch()` and every check through `safe()`, so a failure becomes an
UNKNOWN check, never a crash, never a missing report, and never a PASS.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import checks
from websockets.sync.client import connect

VERSION = "0.1.18"
SCHEMA = 1
SUPERVISOR = "http://supervisor"
CORE_WS = "ws://supervisor/core/websocket"
OPTIONS_FILE = "/data/options.json"
MEMORY_FILE = "/data/memory.json"
STATUS_ENTITY = "sensor.housekeeper_status"
DETAIL_ITEMS, DETAIL_NAME_CHARS, DETAIL_MAX_BYTES = 12, 60, 6000   # the sensor's `checks` attribute stays small
CORE_RETRY_S = 60
STATUS_WATCH_S = 180   # between runs: is our sensor still there? (an HA restart wipes it)
MIN_KEY_LEN = 32

log = logging.getLogger("housekeeper")


# ---------------------------------------------------------------- IO

class Core:
    """Minimal Core websocket client (through the Supervisor proxy). Every recv is timed."""

    def __init__(self, token):
        self.ws = connect(CORE_WS, open_timeout=15, max_size=64 * 1024 * 1024)
        if json.loads(self.ws.recv(timeout=15)).get("type") != "auth_required":
            raise RuntimeError("core websocket: unexpected greeting")
        self.ws.send(json.dumps({"type": "auth", "access_token": token}))
        reply = json.loads(self.ws.recv(timeout=15))
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"core websocket auth failed: {reply.get('type')}")
        self.ha_version = reply.get("ha_version")
        self.next_id = 1

    def call(self, msg_type, **kwargs):
        mid = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": mid, "type": msg_type, **kwargs}))
        while True:
            reply = json.loads(self.ws.recv(timeout=60))
            if reply.get("id") == mid:
                break
        if not reply.get("success"):
            raise RuntimeError(f"{msg_type}: {reply.get('error')}")
        return reply.get("result")

    def close(self):
        self.ws.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send the bearer key and turn the POST into a GET."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method, url, token=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def fetch(label, fn, errors):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — any failure becomes an UNKNOWN downstream
        errors[label] = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("read %s failed: %s", label, errors[label])
        return None


def safe(cid, fn, *args, **kwargs):
    """Run one check; a bug or surprise in it becomes UNKNOWN for that check only."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        log.exception("check %s raised", cid)
        return checks.unknown(cid, f"check raised {type(exc).__name__}")


# ---------------------------------------------------------------- options

def central_url_allowed(url):
    """https always; plain http only to a private / tailnet address (MVP on the LAN)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https":
        return True
    if parts.scheme != "http" or not parts.hostname:
        return False
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts.hostname.endswith((".local", ".ts.net"))
    return ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def load_memory(path=MEMORY_FILE):
    """The agent's own memory across runs (e.g. when a device first went silent). Never fatal."""
    try:
        with open(path) as f:
            mem = json.load(f)
        return mem if isinstance(mem, dict) else {}
    except (OSError, ValueError):
        return {}


def save_memory(mem, path=MEMORY_FILE):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(mem, f)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save memory: %s", exc)


def parse_watch_entities(raw):
    """`watch_entities` form fields: entity, bad_states (comma-separated), for_minutes."""
    rules = []
    for w in raw or []:
        if not isinstance(w, dict) or not isinstance(w.get("entity"), str):
            rules.append({"invalid": str(w)[:80], "error": "needs an entity"})
            continue
        bad = [b.strip() for b in str(w.get("bad_states", "")).split(",") if b.strip()]
        rules.append({"entity": w["entity"].strip(), "bad": bad, "for_min": int(w.get("for_minutes", 60))})
    return rules


def parse_watch(raw):
    """Legacy `watch`: each entry a JSON object string. Bad entries are kept as errors -> UNKNOWN."""
    rules = []
    for w in raw or []:
        try:
            rule = json.loads(w) if isinstance(w, str) else w
            if not isinstance(rule, dict) or not isinstance(rule.get("entity"), str):
                raise ValueError("needs an object with 'entity'")
            if not isinstance(rule.get("bad", []), list):
                raise ValueError("'bad' must be a list")
            rules.append(rule)
        except (ValueError, TypeError) as exc:
            rules.append({"invalid": str(w)[:80], "error": str(exc)})
    return rules


# ---------------------------------------------------------------- one cycle

def connect_core(token, errors):
    core = fetch("core/websocket", lambda: Core(token), errors)
    if core is None:
        # One retry: a cycle that lands on an HA restart/update must not page as "Core down".
        time.sleep(CORE_RETRY_S)
        errors.pop("core/websocket", None)
        core = fetch("core/websocket", lambda: Core(token), errors)
    return core


def gather(token):
    errors = {}
    data = {"errors": errors}
    sup = fetch("supervisor/resolution", lambda: http("GET", f"{SUPERVISOR}/resolution/info", token), errors)
    data["resolution"] = sup.get("data") if isinstance(sup, dict) else None
    host = fetch("supervisor/host", lambda: http("GET", f"{SUPERVISOR}/host/info", token), errors)
    data["host"] = host.get("data") if isinstance(host, dict) else None
    core = connect_core(token, errors)
    if core is None:
        return data, None
    try:
        data["states"] = fetch("get_states", lambda: core.call("get_states"), errors)
        data["entity_registry"] = fetch("entity_registry", lambda: core.call("config/entity_registry/list"), errors)
        data["device_registry"] = fetch("device_registry", lambda: core.call("config/device_registry/list"), errors)
        data["backup_info"] = fetch("backup/info", lambda: core.call("backup/info"), errors)
        data["repairs"] = fetch("repairs", lambda: (core.call("repairs/list_issues") or {}).get("issues"), errors)
        data["notifications"] = fetch("notifications", lambda: core.call("persistent_notification/get"), errors)
        data["config"] = fetch("get_config", lambda: core.call("get_config"), errors)
        data["config_entries"] = fetch("config_entries", lambda: core.call("config_entries/get"), errors)
        data["system_log"] = fetch("system_log", lambda: core.call("system_log/list"), errors)
        data["flap_candidates"] = checks.flap_candidates(data["entity_registry"], data["device_registry"])
        if data["flap_candidates"]:
            start = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            ids = sorted(data["flap_candidates"])
            data["history_24h"] = fetch("history", lambda: core.call("history/history_during_period", start_time=start,
                                                                     entity_ids=ids, minimal_response=True,
                                                                     no_attributes=True), errors)
        gather_alarms(core, data, errors)
        gather_selfheal(core, data, errors)
        return data, core.ha_version
    finally:
        core.close()


def gather_alarms(core, data, errors):
    """Alarm events (DEC-10 processes): 24 h of state history for the entities labelled `alarm_process`, and the
    logbook since the last good read (the 'Alarm: <Process>' lines carry each alarm's message). Read-only; skipped
    entirely in a home with no labelled processes. A failed logbook read leaves `alarm_logbook` None (messages are
    then missing this run, the states still count)."""
    data["alarm_entities"] = checks.alarm_entities(data.get("entity_registry"))
    if not data["alarm_entities"]:
        return
    now = datetime.now(UTC)
    start = (now - timedelta(hours=checks.ALARM_WINDOW_H)).isoformat()
    ids = sorted(data["alarm_entities"])
    data["alarm_history"] = fetch("alarm_history", lambda: core.call("history/history_during_period",
                                                                     start_time=start, entity_ids=ids,
                                                                     minimal_response=True, no_attributes=True),
                                  errors)
    since = checks.alarm_log_since(load_memory(), now).isoformat()
    events = fetch("logbook", lambda: core.call("logbook/get_events", start_time=since, end_time=now.isoformat()),
                   errors)
    data["alarm_logbook"] = {"events": events, "to": now.isoformat()} if isinstance(events, list) else None


def gather_selfheal(core, data, errors):
    """Self-heal logbook lines (blueprints/automation/housekeeper). Reuses the alarm read when it ran; otherwise one
    incremental logbook read of its own, since the last good one (kept in memory, like the alarm lines)."""
    if data.get("alarm_logbook") is not None:
        data["selfheal_logbook"] = data["alarm_logbook"]
        return
    now = datetime.now(UTC)
    mem = (load_memory().get("selfheal_log") or {})
    since = checks.alarm_log_since({"alarm_log": mem}, now).isoformat()
    events = fetch("logbook_selfheal", lambda: core.call("logbook/get_events", start_time=since,
                                                         end_time=now.isoformat()), errors)
    data["selfheal_logbook"] = {"events": events, "to": now.isoformat()} if isinstance(events, list) else None


def run_checks(data, opts, now, memory=None):
    s_all = data.get("states")
    memory = {} if memory is None else memory
    ents_all = data.get("entity_registry")
    devs = data.get("device_registry")
    # Things labelled Maintenance are left out of every device/automation check (devices.maintenance lists them).
    parked = checks.labelled(ents_all, devs, checks.MAINTENANCE_LABEL)
    keep_on = checks.labelled(ents_all, devs, checks.KEEP_ON_LABEL)
    rarely = checks.labelled(ents_all, devs, checks.RARELY_USED_LABEL)
    s = [x for x in s_all if x.get("entity_id") not in parked] if isinstance(s_all, list) else s_all
    ents = [e for e in ents_all if e.get("entity_id") not in parked] if isinstance(ents_all, list) else ents_all
    flaps = {k: v for k, v in (data.get("flap_candidates") or {}).items() if k not in parked}
    out = []
    if data.get("errors", {}).get("core/websocket"):
        out.append(checks.result("core.reachable", checks.FAIL,
                                 f"HA Core not reachable after retry: {data['errors']['core/websocket']}"))
    else:
        out.append(checks.result("core.reachable", checks.PASS, "HA Core answering"))
    out += [
        safe("supervisor.health", checks.check_supervisor, data.get("resolution")),
        safe("backup.offsite", checks.check_backup_offsite, data.get("backup_info"), now, states=s_all),
        safe("backup.last_attempt", checks.check_backup_last_attempt, data.get("backup_info"), now),
        safe("ha.repairs", checks.check_repairs, data.get("repairs")),
        safe("ha.notifications", checks.check_notifications, data.get("notifications")),
        safe("ha.updates", checks.check_updates, s_all, checks.integration_domains(data.get("config"))),
        safe("devices.silent", checks.check_silent_devices, s, ents, devs, now,
             silent_h=opts.get("silent_hours", 24), exceptions=opts.get("silent_exceptions", []),
             memory=memory.setdefault("silent_since", {})),
        safe("devices.battery", checks.check_batteries, s, ents, warn_pct=opts.get("battery_warn_pct", 25),
             now=now, stale_h=opts.get("silent_hours", 24)),
        safe("devices.flapping", checks.check_flapping, data.get("history_24h"), flaps,
             platforms={e.get("entity_id"): e.get("platform") for e in ents if isinstance(e, dict)}
             if isinstance(ents, list) else None),
        safe("automations.stopped", checks.check_automations, s, now, memory.setdefault("triggers", {}),
             exceptions=list(opts.get("automation_exceptions", [])) + sorted(rarely)),
        safe("system.disk", checks.check_disk, data.get("host")),
        safe("ha.integrations", checks.check_integrations, data.get("config_entries"),
             memory.setdefault("failing_integrations", {}), exceptions=opts.get("integration_exceptions", [])),
        safe("ha.log_errors", checks.check_log_errors, data.get("system_log"), now,
             threshold=opts.get("log_error_threshold", 10)),
        safe("devices.zero_power", checks.check_zero_power, s, ents, now, memory.setdefault("zero_power", {}),
             hours_needed=opts.get("zero_power_hours", 6),
             exceptions=list(opts.get("zero_power_exceptions", [])) + sorted(keep_on),
             found=data.setdefault("idle_outlets", {})),
        safe("devices.maintenance", checks.check_maintenance, ents_all, devs, s_all, now,
             memory.setdefault("maintenance", {}), max_days=opts.get("maintenance_days", 14)),
        safe("alarms.events", checks.check_alarm_events, s_all,
             data.get("alarm_entities", checks.alarm_entities(ents_all)),
             data.get("alarm_history"), data.get("alarm_logbook"), now, memory.setdefault("alarm_log", {}),
             share_messages=opts.get("share_alarm_messages", True)),
        safe("selfheal.events", checks.check_selfheal_events, data.get("selfheal_logbook"), now,
             memory.setdefault("selfheal_log", {})),
    ]
    if opts.get("facts_entity"):
        out.append(safe("house.facts", checks.check_house_facts, s_all, opts["facts_entity"], now))
    for rule in opts.get("watch", []):
        if "invalid" in rule:
            out.append(checks.unknown("watch.invalid", f"bad watch entry {rule['invalid']!r}: {rule['error']}"))
            continue
        out.append(safe(f"watch.{rule['entity']}", checks.check_watched_entity, s_all, rule, now,
                        memory.setdefault("watch_bad_since", {})))
    if not opts.get("fix_offers"):                # off (the default): no entity ids for fix offers leave the house
        for r in out:
            if isinstance(r.get("evidence"), dict):
                r["evidence"].pop("fixable", None)
    return out


def build_card(opts, ha_version, results, errors, now, published=None):
    card = {
        "schema": SCHEMA,
        "report_id": str(uuid.uuid4()),
        "site": opts["site_id"],
        "agent": VERSION,
        "ha": ha_version,
        "ts": now.isoformat(),
        "checks": results,
        "read_errors": errors,
    }
    if opts.get("fix_offers"):
        # The offer ids now in our sensor: central pushes a "Watson can fix" button only for these.
        card["fix_offers_published"] = [str(i) for i in (published or [])][:FIX_OFFERS_MAX]
    return card


FIX_KINDS = {"reload_entry": "entity_id", "reload_stuck": "entry_id", "restart_app": "slug", "outlet_off": "entity_id"}
FIX_TARGET = {"entity_id": re.compile(r"^[a-z0-9_]{1,40}\.[a-z0-9_]{1,200}$"),
              "entry_id": re.compile(r"^[A-Za-z0-9_-]{1,64}$"), "slug": re.compile(r"^[a-z0-9_-]{1,64}$")}
FIX_ID = re.compile(r"^[A-Za-z0-9]{8,64}$")
FIX_OFFERS_MAX = 5


def fix_offers(reply, opts):
    """Central's offers for the sensor's `fix_offers` attribute, which the house's HA package (watson_fix) checks a
    button's id against. Empty when the option is off or central sent nothing (so every button stops working); each
    offer is re-validated here, only the known kinds with a well-formed target get through."""
    if not opts.get("fix_offers") or not isinstance(reply, dict) or not isinstance(reply.get("fix_offers"), list):
        return []
    out = []
    for o in reply["fix_offers"][:FIX_OFFERS_MAX]:
        if not isinstance(o, dict) or o.get("kind") not in FIX_KINDS:
            continue
        field = FIX_KINDS[o["kind"]]
        oid, target, expires = o.get("id"), o.get(field), o.get("expires")
        if not (isinstance(oid, str) and FIX_ID.match(oid) and isinstance(target, str)
                and FIX_TARGET[field].match(target) and isinstance(expires, str)):
            continue
        try:
            datetime.fromisoformat(expires)
        except ValueError:
            continue
        out.append({"id": oid, "kind": o["kind"], field: target, "label": str(o.get("label") or "")[:60],
                    "expires": expires})
    return out


def household_list(reply, results):
    """Central's answer wins, including an empty list (= everything silenced or fine).
    Only when central could not be reached do we fall back to the local checks: household-facing ones only, as
    central does (an operator check's wording, like the Maintenance reminder, is not for the household)."""
    if isinstance(reply, dict) and isinstance(reply.get("household"), list):
        return [str(m)[:200] for m in reply["household"][:10]]
    return [r["household"] for r in results
            if r.get("household") and r.get("audience") == "household"
            and r.get("status") in (checks.WARN, checks.FAIL)]


def detail_items(check):
    """The named things behind one check, for the `checks` attribute: [{name, days?, detail?, until?, over?}]."""
    ev = check.get("evidence") or {}

    def rows(key):
        return [x for x in ev.get(key) or [] if isinstance(x, dict) and x.get("name")]

    cid, out = check.get("id"), []
    if cid == "devices.silent":
        out = [{"name": x["name"], "days": round(x["silent_h"] / 24, 1)} for x in rows("silent")]
        out += [{"name": x["name"], "days": round(x["h"] / 24, 1)} for x in rows("not_heard")]
    elif cid == "devices.battery":
        out = [{"name": x["name"], "detail": f"{x['pct']:.0f} %"} for x in rows("low")]
        out += [{"name": x["name"], "days": round(x["h"] / 24, 1), "detail": f"last read {x['last_pct']:.0f} %"}
                for x in rows("not_heard")]
    elif cid == "devices.flapping":
        out = [{"name": x["name"], "detail": f"dropped off {x['drops_24h']}x in 24 h"} for x in rows("flapping")]
    elif cid == "devices.zero_power":
        out = [{"name": x["name"], "days": round(x["hours"] / 24, 1), "detail": "on at 0 W"}
               for x in rows("zero_power")]
    elif cid == "automations.stopped":
        out = [{"name": x["name"], "days": x["silent_d"]} for x in rows("stopped")]
        out += [{"name": str(n), "detail": "not loaded"} for n in ev.get("unavailable") or []]
    elif cid == "ha.integrations":
        out = [{"name": x["name"], "detail": str(x.get("state", "")).replace("_", " ")} for x in rows("failing")]
    elif cid == "ha.repairs":
        out = [{"name": str(n)} for n in ev.get("issues") or []]
    elif cid == "ha.log_errors":
        out = [{"name": str(n), "detail": f"{c} errors in 24 h"} for n, c in (ev.get("by_integration") or {}).items()]
    elif cid == checks.MAINTENANCE_CHECK:
        out = [{k: x[k] for k in ("name", "days", "until", "over") if k in x} for x in rows("parked")]
    return out


def check_details(reply, results):
    """What is behind Watson's one-liners, for the sensor's `checks` attribute (the board's House health panel): every
    check that is not PASS, plus what is parked. Stays in this house's HA. `silenced` = central has an exception or a
    snooze on it. Bounded, because HA's recorder refuses attributes over 16 KB: the longest lists lose items first."""
    told = reply.get("household") if isinstance(reply, dict) else None
    told = [str(m)[:300] for m in told] if isinstance(told, list) else None
    rank = {checks.FAIL: 0, checks.WARN: 1, checks.UNKNOWN: 2}
    out = []
    for c in sorted(results, key=lambda r: rank.get(r.get("status"), 3)):
        items = detail_items(c)
        if c.get("status") == checks.PASS and not (c.get("id") == checks.MAINTENANCE_CHECK and items):
            continue
        shown = [{**i, "name": str(i["name"])[:DETAIL_NAME_CHARS]} for i in items[:DETAIL_ITEMS]]
        out.append({"id": c.get("id"), "status": c.get("status"),
                    "household": str(c["household"])[:200] if c.get("household") else None,
                    "reason": str(c.get("reason") or "")[:240],
                    "silenced": bool(told is not None and c.get("household") and c.get("audience") == "household"
                                     and c.get("status") in (checks.WARN, checks.FAIL)
                                     and str(c["household"])[:300] not in told),
                    "items": shown, "more": len(items) - len(shown)})
    while len(json.dumps(out)) > DETAIL_MAX_BYTES:
        longest = max(out, key=lambda c: len(c["items"]))
        if longest["items"]:
            longest["items"].pop()
            longest["more"] += 1
        else:
            out.pop()                                   # nothing left to trim: drop the least urgent check
    return out


def house_status(reply, results, persona="Watson", idle_outlets=None, offers=None):
    """Our sensor's state + attributes, so the household sees their house assistant's status in HA.
    `idle_outlets`: outlets on at ~0 W for hours (not Keep on / Maintenance), for the house's own turn-off self-heal
    (blueprint `turn_off_idle_outlets`) to act on. The App only lists them; Home Assistant decides and switches.
    `checks`: the detail behind `messages` (check_details).
    `fix_offers`: what central offered to fix (fix_offers()); HA's watson_fix package acts on a tapped id only if it
    is listed here. The App never acts on them itself."""
    messages = household_list(reply, results)
    state = "attention" if messages else "all_good"
    summary = "all good" if not messages else f"{len(messages)} things need attention"
    body = {"state": state, "attributes": {
        "friendly_name": persona,
        "icon": "mdi:shield-home" if state == "all_good" else "mdi:shield-alert",
        "messages": messages,
        "summary": f"{persona}: {summary}",
        # Central's plain-words weekly note (what got fixed, what's coming, e.g. a battery running out).
        "note": str(reply.get("note") or "")[:500] if isinstance(reply, dict) else "",
        "checked_at": datetime.now(UTC).isoformat(),
        "central_reached": reply is not None,
        "idle_outlets": [{"switch": k, "meter": v["meter"], "hours": v["hours"]}
                         for k, v in sorted((idle_outlets or {}).items())][:30],
        "checks": check_details(reply, results),
        "fix_offers": list(offers or []),
    }}
    return body


def write_house_status(token, body):
    """The one write: our own sensor. Hourly from `cycle`, and again from `repost_status` after an HA restart."""
    http("POST", f"{SUPERVISOR}/core/api/states/{STATUS_ENTITY}", token, body)


def status_missing(token):
    """True when HA answers but our sensor is gone (404) or unknown: a state set over the API does not survive an HA
    restart, so the dashboards would show no Watson until the next run. False when it is there. None when HA is not
    answering (restarting, or the Supervisor proxy says 502): just wait. One read-only GET, never raises."""
    try:
        st = http("GET", f"{SUPERVISOR}/core/api/states/{STATUS_ENTITY}", token, timeout=10)
    except urllib.error.HTTPError as exc:
        return True if exc.code == 404 else None
    except Exception:  # noqa: BLE001 — HA down / restarting
        return None
    if not isinstance(st, dict):
        return None
    return st.get("state") in (None, "unknown", "unavailable")


def repost_status(token, body):
    """Put the last status back, unchanged, once HA is answering again without it. No checks run, nothing goes to
    central. Nothing to do before the first run has written one. Never raises."""
    if not body or status_missing(token) is not True:
        return False
    try:
        write_house_status(token, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not re-post %s: %s", STATUS_ENTITY, exc)
        return False
    log.info("status re-posted after Home Assistant restart")
    return True


PUBLISHED_KEY = "fix_offers_published"


def remember_published(ids, path=MEMORY_FILE):
    """The offer ids written into our sensor this run, echoed in the next card (build_card)."""
    mem = load_memory(path)
    mem[PUBLISHED_KEY] = list(ids)
    save_memory(mem, path)


def cycle(opts, token):
    now = datetime.now(UTC)
    data, ha_version = gather(token)
    memory = load_memory()
    results = run_checks(data, opts, now, memory)
    if data.get("states") is not None:  # don't overwrite memory with nothing when HA was unreadable
        save_memory(memory)
    card = build_card(opts, ha_version, results, data["errors"], now, memory.get(PUBLISHED_KEY))
    reply = None
    try:
        reply = http("POST", opts["central_url"].rstrip("/") + "/v1/report", opts["site_key"], card)
        log.info("report sent: %s", (reply or {}).get("score"))
    except Exception as exc:  # noqa: BLE001
        log.error("could not reach central: %s", exc)
    status, published = None, []
    try:
        offers = fix_offers(reply, opts)
        status = house_status(reply, results, opts.get("persona") or "Watson", data.get("idle_outlets"), offers)
        write_house_status(token, status)
        published = [o["id"] for o in offers]
    except Exception as exc:  # noqa: BLE001 — kept anyway: repost_status puts it there once HA answers
        log.error("could not write %s: %s", STATUS_ENTITY, exc)
    if published or memory.get(PUBLISHED_KEY):
        remember_published(published)
    if opts.get("healthchecks_url"):
        try:
            _opener.open(opts["healthchecks_url"], timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("healthchecks ping failed: %s", exc)
    return card, reply, status


def load_options(path=OPTIONS_FILE):
    with open(path) as f:
        opts = json.load(f)
    if not central_url_allowed(opts.get("central_url", "")):
        raise SystemExit("central_url must be https:// (plain http only to a private/tailnet address)")
    if len(opts.get("site_key", "")) < MIN_KEY_LEN:
        raise SystemExit(f"site_key must be at least {MIN_KEY_LEN} characters")
    opts["watch"] = parse_watch_entities(opts.get("watch_entities")) + parse_watch(opts.get("watch"))
    return opts


def stop(signum, _frame):
    """The agent is PID 1 in its container (init: false), where SIGTERM is ignored unless handled: without this,
    every App stop/restart/update waited 10 s and ended in SIGKILL (exit 137, App state "error")."""
    log.info("stopping (signal %d)", signum)
    raise SystemExit(0)


def wait_for_next_run(token, status, started, interval):
    """Sleep until the next run (at least 60 s), glancing at our sensor every STATUS_WATCH_S on the way."""
    deadline = max(started + interval, time.monotonic() + 60)
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(STATUS_WATCH_S, left))
        if time.monotonic() < deadline:
            repost_status(token, status)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    socket.setdefaulttimeout(60)
    opts = load_options()
    token = os.environ["SUPERVISOR_TOKEN"]
    interval = max(5, int(opts.get("interval_minutes", 60))) * 60
    log.info("Housekeeper Agent %s for site %s, every %d min", VERSION, opts["site_id"], interval // 60)
    status = None   # the last status we wrote (in memory: a fresh App start runs a cycle straight away)
    while True:
        started = time.monotonic()
        try:
            _, _, written = cycle(opts, token)
            status = written or status
        except Exception:
            log.exception("cycle failed")
        wait_for_next_run(token, status, started, interval)


if __name__ == "__main__":
    main()
