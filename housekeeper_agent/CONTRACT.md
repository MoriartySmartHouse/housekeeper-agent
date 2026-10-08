# Contract — everything the agent reads from Home Assistant

When an HA or Supervisor release changes any of these, the matching check must go UNKNOWN (never PASS).
Review each HA release's "Backward-incompatible changes" against this list.

| # | Source | Call | Fields relied on | Check |
|---|---|---|---|---|
| 1 | Supervisor | `GET /resolution/info` | `data.unhealthy`, `data.unsupported`, `data.issues[].type` | supervisor.health |
| 2 | Core WS | `auth` via `ws://supervisor/core/websocket` | `auth_ok`, `ha_version` | core.reachable |
| 3 | Core WS | `get_states` | `entity_id`, `state`, `last_changed`, `attributes.device_class`, `attributes.unit_of_measurement`, `attributes.friendly_name` | updates, silent, battery, watch |
| 4 | Core WS | `config/entity_registry/list` | `entity_id`, `device_id`, `platform`, `disabled_by` | silent, battery |
| 5 | Core WS | `config/device_registry/list` | `id`, `name`, `name_by_user` | silent |
| 6 | Core WS | `backup/info` | `backups[].date`, `backups[].agents` (dict) or `agent_ids` (list), `last_attempted_automatic_backup`, `last_completed_automatic_backup` | backup.offsite, backup.last_attempt |
| 7 | Core WS | `repairs/list_issues` | `issues[].domain`, `issue_id`, `severity`, `ignored`, `dismissed_version` | ha.repairs |
| 8 | Core WS | `persistent_notification/get` | list (or dict) of `notification_id`, `title` | ha.notifications |
| 9 | Core REST | `POST /api/states/sensor.housekeeper_status` | (write — our own sensor only) | house status |
| 10 | Conventions | `update.*` state `on` = update available; battery = `device_class: battery` + `%`; z2m entities have platform `mqtt` | | updates, battery, silent |
| 11 | Conventions | z2m `sensor.*_last_seen` state = ISO timestamp of last message (when enabled) | | silent |
| 12 | Behaviour | HA resets `last_changed` of unavailable entities on restart → agent keeps `/data/memory.json` | | silent |
