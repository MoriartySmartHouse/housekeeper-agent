"""Housekeeper Agent — runs inside an HA App, hourly.

read (Supervisor + Core via the Supervisor proxy) -> checks -> report card -> POST to central
-> write the household status back into this house's HA as sensor.housekeeper_status.

Read-only towards HA except that one sensor it owns. Every read goes through `fetch()` so a
failure becomes an UNKNOWN check, never a crash and never a PASS.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.request
import uuid
from datetime import datetime, timezone

from websockets.sync.client import connect

import checks

VERSION = "0.1.0"
SCHEMA = 1
SUPERVISOR = "http://supervisor"
CORE_WS = "ws://supervisor/core/websocket"
OPTIONS_FILE = "/data/options.json"
STATUS_ENTITY = "sensor.housekeeper_status"

log = logging.getLogger("housekeeper")


# ---------------------------------------------------------------- IO

class Core:
    """Minimal Core websocket client (through the Supervisor proxy)."""

    def __init__(self, token):
        self.ws = connect(CORE_WS, open_timeout=15, max_size=64 * 1024 * 1024)
        assert json.loads(self.ws.recv())["type"] == "auth_required"
        self.ws.send(json.dumps({"type": "auth", "access_token": token}))
        reply = json.loads(self.ws.recv())
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


def http(method, url, token=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def fetch(label, fn, errors):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — any failure becomes an UNKNOWN downstream
        errors[label] = f"{type(exc).__name__}: {exc}"
        log.warning("read %s failed: %s", label, errors[label])
        return None


# ---------------------------------------------------------------- one cycle

def gather(token):
    errors = {}
    data = {"errors": errors}
    sup = fetch("supervisor/resolution", lambda: http("GET", f"{SUPERVISOR}/resolution/info", token), errors)
    data["resolution"] = sup.get("data") if isinstance(sup, dict) else None
    core = fetch("core/websocket", lambda: Core(token), errors)
    if core is None:
        return data, None
    try:
        data["states"] = fetch("get_states", lambda: core.call("get_states"), errors)
        data["entity_registry"] = fetch("entity_registry", lambda: core.call("config/entity_registry/list"), errors)
        data["device_registry"] = fetch("device_registry", lambda: core.call("config/device_registry/list"), errors)
        data["backup_info"] = fetch("backup/info", lambda: core.call("backup/info"), errors)
        data["repairs"] = fetch("repairs", lambda: (core.call("repairs/list_issues") or {}).get("issues"), errors)
        data["notifications"] = fetch("notifications", lambda: core.call("persistent_notification/get"), errors)
        return data, core.ha_version
    finally:
        core.close()


def run_checks(data, opts, now):
    s = data.get("states")
    out = []
    if data.get("errors", {}).get("core/websocket"):
        out.append(checks.result("core.reachable", checks.FAIL,
                                 f"HA Core not reachable: {data['errors']['core/websocket']}"))
    else:
        out.append(checks.result("core.reachable", checks.PASS, "HA Core answering"))
    out += [
        checks.check_supervisor(data.get("resolution")),
        checks.check_backup_offsite(data.get("backup_info"), now),
        checks.check_backup_last_attempt(data.get("backup_info"), now),
        checks.check_repairs(data.get("repairs")),
        checks.check_notifications(data.get("notifications")),
        checks.check_updates(s),
        checks.check_silent_devices(s, data.get("entity_registry"), data.get("device_registry"), now,
                                    silent_h=opts.get("silent_hours", 24),
                                    exceptions=opts.get("silent_exceptions", [])),
        checks.check_batteries(s, data.get("entity_registry"), warn_pct=opts.get("battery_warn_pct", 25)),
    ]
    for rule in opts.get("watch", []):
        out.append(checks.check_watched_entity(s, rule, now))
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


def write_house_status(token, reply, results, persona="Watson"):
    """The one write: our own sensor, so the household sees their house assistant's status in HA."""
    messages = (reply or {}).get("household") or [r["household"] for r in results if r.get("household")]
    state = "attention" if messages else "all_good"
    body = {"state": state, "attributes": {
        "friendly_name": persona,
        "icon": "mdi:shield-home" if state == "all_good" else "mdi:shield-alert",
        "messages": messages,
        "summary": f"{persona}: " + ((reply or {}).get("summary")
                                     or ("all good" if not messages else f"{len(messages)} things need attention")),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "central_reached": reply is not None,
    }}
    http("POST", f"{SUPERVISOR}/core/api/states/{STATUS_ENTITY}", token, body)


def cycle(opts, token):
    now = datetime.now(timezone.utc)
    data, ha_version = gather(token)
    results = run_checks(data, opts, now)
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
            urllib.request.urlopen(opts["healthchecks_url"], timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("healthchecks ping failed: %s", exc)
    return card, reply


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    socket.setdefaulttimeout(60)
    with open(OPTIONS_FILE) as f:
        opts = json.load(f)
    opts["watch"] = [json.loads(w) if isinstance(w, str) else w for w in opts.get("watch", [])]
    token = os.environ["SUPERVISOR_TOKEN"]
    interval = max(5, int(opts.get("interval_minutes", 60))) * 60
    log.info("Housekeeper Agent %s for site %s, every %d min", VERSION, opts["site_id"], interval // 60)
    while True:
        try:
            cycle(opts, token)
        except Exception:  # noqa: BLE001 — never die; central will see silence if this keeps failing
            log.exception("cycle failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
