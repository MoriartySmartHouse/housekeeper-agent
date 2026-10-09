"""Write guard: the agent may only (1) POST its own status sensor and (2) POST the report card to
central. Any other write path — service calls, other entities, config, Supervisor actions — fails
the build. This turns "read-only" from a promise into a checked property."""
import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SRC = {p.name: p.read_text(encoding="utf-8") for p in APP.glob("*.py")}

ALLOWED_POSTS = {
    "f'{SUPERVISOR}/core/api/states/{STATUS_ENTITY}'",
    "opts['central_url'].rstrip('/') + '/v1/report'",
}


def test_status_entity_is_constant_and_ours():
    assert re.search(r'^STATUS_ENTITY = "sensor\.housekeeper_status"$', SRC["agent.py"], re.M)


def test_only_allowed_http_writes():
    tree = ast.parse(SRC["agent.py"])
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "http":
            method = node.args[0].value if isinstance(node.args[0], ast.Constant) else "?"
            if method != "GET":
                found.append(ast.unparse(node.args[1]))
    assert found and set(found) <= ALLOWED_POSTS, found


def test_no_service_calls_or_mutating_ws_commands():
    banned = [r"call_service", r"/api/services", r"/services/", r'"execute_script"', r"fire_event",
              r"/api/config", r"config_entries/", r"/addons/", r"/apps/", r"/core/restart", r"/host/",
              r"/backups/new", r"/store", r'call\(\s*"(?!(get_states|config/entity_registry/list|'
              r'config/device_registry/list|backup/info|repairs/list_issues|persistent_notification/get|'
              r'history/history_during_period)")']
    for name, src in SRC.items():
        for pat in banned:
            assert not re.search(pat, src), f"{name}: forbidden pattern {pat!r}"


def test_websocket_commands_are_the_contract_list():
    cmds = set(re.findall(r'core\.call\("([^"]+)"', SRC["agent.py"]))
    assert cmds == {"get_states", "config/entity_registry/list", "config/device_registry/list",
                    "backup/info", "repairs/list_issues", "persistent_notification/get",
                    "history/history_during_period"}
