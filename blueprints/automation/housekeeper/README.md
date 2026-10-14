# Watson self-heal blueprints

Home Assistant fixes the safe, common problems itself; the Housekeeper App reports what happened. The App never
acts - these run in your own Home Assistant, only where you install them.

| Blueprint | Does | Typical use |
|---|---|---|
| `reload_unavailable_device.yaml` | a device unavailable for N min -> reload its integration | a Wi-Fi bulb or plug that drops off |
| `reload_stuck_integration.yaml` | an integration stuck "retrying setup" -> reload it | a cloud or LAN integration that hung |
| `restart_app_when_down.yaml` | an App's health sensor reads down for N min -> restart the App | Zigbee2MQTT bridge offline |
| `turn_off_idle_outlets.yaml` | Watson lists outlets on with nothing drawing for 6 h -> turn them off | an empty socket left on |

**Rules every one follows:** one try per cooldown (no loops); it checks the result and writes one logbook line
`Watson self-heal | <target>: fixed|still down - <what it did>`. Only `turn_off_idle_outlets` switches anything,
and only **off**, never an outlet labelled **Keep on** or **Maintenance**, and only after re-checking the meter. The device blueprint
cannot pick locks, climate, alarms, valves, sirens or water heaters, and refuses whole-mesh integrations (MQTT /
Zigbee2MQTT, ZHA, Z-Wave, deCONZ, Matter) - reloading those drops every device on the mesh at once, so use the App
restart blueprint for them instead.

**Install:** Settings > Automations & Scenes > Blueprints > Import Blueprint, paste the file's GitHub URL; then
"Create automation" from it. Watson starts reporting on its next hourly check (App version with *Self-heals*).
