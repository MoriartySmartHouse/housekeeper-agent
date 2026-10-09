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
import signal
import socket
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import checks
from websockets.sync.client import connect

VERSION = "0.1.7"
SCHEMA = 1
SUPERVISOR = "http://supervisor"
CORE_WS = "ws://supervisor/core/websocket"
OPTIONS_FILE = "/data/options.json"
MEMORY_FILE = "/data/memory.json"
STATUS_ENTITY = "sensor.housekeeper_status"
CORE_RETRY_S = 60
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
        return data, core.ha_version
    finally:
        core.close()


def run_checks(data, opts, now, memory=None):
    s = data.get("states")
    memory = {} if memory is None else memory
    ents = data.get("entity_registry")
    out = []
    if data.get("errors", {}).get("core/websocket"):
        out.append(checks.result("core.reachable", checks.FAIL,
                                 f"HA Core not reachable after retry: {data['errors']['core/websocket']}"))
    else:
        out.append(checks.result("core.reachable", checks.PASS, "HA Core answering"))
    out += [
        safe("supervisor.health", checks.check_supervisor, data.get("resolution")),
        safe("backup.offsite", checks.check_backup_offsite, data.get("backup_info"), now, states=s),
        safe("backup.last_attempt", checks.check_backup_last_attempt, data.get("backup_info"), now),
        safe("ha.repairs", checks.check_repairs, data.get("repairs")),
        safe("ha.notifications", checks.check_notifications, data.get("notifications")),
        safe("ha.updates", checks.check_updates, s, checks.integration_domains(data.get("config"))),
        safe("devices.silent", checks.check_silent_devices, s, ents, data.get("device_registry"), now,
             silent_h=opts.get("silent_hours", 24), exceptions=opts.get("silent_exceptions", []),
             memory=memory.setdefault("silent_since", {})),
        safe("devices.battery", checks.check_batteries, s, ents, warn_pct=opts.get("battery_warn_pct", 25)),
        safe("devices.flapping", checks.check_flapping, data.get("history_24h"), data.get("flap_candidates") or {}),
        safe("automations.stopped", checks.check_automations, s, now, memory.setdefault("triggers", {}),
             exceptions=opts.get("automation_exceptions", [])),
        safe("system.disk", checks.check_disk, data.get("host")),
        safe("ha.integrations", checks.check_integrations, data.get("config_entries"),
             memory.setdefault("failing_integrations", {}), exceptions=opts.get("integration_exceptions", [])),
        safe("ha.log_errors", checks.check_log_errors, data.get("system_log"), now,
             threshold=opts.get("log_error_threshold", 10)),
        safe("devices.zero_power", checks.check_zero_power, s, ents, now, memory.setdefault("zero_power", {}),
             hours_needed=opts.get("zero_power_hours", 6), exceptions=opts.get("zero_power_exceptions", [])),
    ]
    for rule in opts.get("watch", []):
        if "invalid" in rule:
            out.append(checks.unknown("watch.invalid", f"bad watch entry {rule['invalid']!r}: {rule['error']}"))
            continue
        out.append(safe(f"watch.{rule['entity']}", checks.check_watched_entity, s, rule, now))
    return out


def build_card(opts, ha_version, results, errors, now):
    return {
        "schema": SCHEMA,
        "report_id": str(uuid.uuid4()),
        "site": opts["site_id"],
        "agent": VERSION,
        "ha": ha_version,
        "ts": now.isoformat(),
        "checks": results,
        "read_errors": errors,
    }


def household_list(reply, results):
    """Central's answer wins, including an empty list (= everything silenced or fine).
    Only when central could not be reached do we fall back to the local checks."""
    if isinstance(reply, dict) and isinstance(reply.get("household"), list):
        return [str(m)[:200] for m in reply["household"][:10]]
    return [r["household"] for r in results
            if r.get("household") and r.get("status") in (checks.WARN, checks.FAIL)]


def write_house_status(token, reply, results, persona="Watson"):
    """The one write: our own sensor, so the household sees their house assistant's status in HA."""
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
    }}
    http("POST", f"{SUPERVISOR}/core/api/states/{STATUS_ENTITY}", token, body)


def cycle(opts, token):
    now = datetime.now(UTC)
    data, ha_version = gather(token)
    memory = load_memory()
    results = run_checks(data, opts, now, memory)
    if data.get("states") is not None:  # don't overwrite memory with nothing when HA was unreadable
        save_memory(memory)
    card = build_card(opts, ha_version, results, data["errors"], now)
    reply = None
    try:
        reply = http("POST", opts["central_url"].rstrip("/") + "/v1/report", opts["site_key"], card)
        log.info("report sent: %s", (reply or {}).get("score"))
    except Exception as exc:  # noqa: BLE001
        log.error("could not reach central: %s", exc)
    try:
        write_house_status(token, reply, results, opts.get("persona") or "Watson")
    except Exception as exc:  # noqa: BLE001
        log.error("could not write %s: %s", STATUS_ENTITY, exc)
    if opts.get("healthchecks_url"):
        try:
            _opener.open(opts["healthchecks_url"], timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("healthchecks ping failed: %s", exc)
    return card, reply


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    socket.setdefaulttimeout(60)
    opts = load_options()
    token = os.environ["SUPERVISOR_TOKEN"]
    interval = max(5, int(opts.get("interval_minutes", 60))) * 60
    log.info("Housekeeper Agent %s for site %s, every %d min", VERSION, opts["site_id"], interval // 60)
    while True:
        started = time.monotonic()
        try:
            cycle(opts, token)
        except Exception:
            log.exception("cycle failed")
        time.sleep(max(60, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
