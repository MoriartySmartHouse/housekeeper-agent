# Housekeeper Agent

Once an hour this App checks the health of your home — backups, repairs, waiting updates, devices that
have stopped reporting, low batteries — and sends a short report card to the Housekeeper service run by the
person who looks after your system. It shows the result in your Home Assistant as a status from **Watson**, your
house assistant (`sensor.housekeeper_status`; you can rename Watson in the settings).

## What it can see, and what leaves your home
- To read your system it uses the access Home Assistant gives every App, which is **administrator-level
  read access to Home Assistant**. The App's code only reads, except for writing its own *House status*
  sensor. The code is public in the repository above.
- **What leaves your home:** check results only — e.g. "backup 8 h old", "3 updates waiting",
  "Front door lock battery 18 %", device names and counts. **Never** camera images, locations,
  history, or your passwords.
- To stop it at any time: stop or uninstall this App.

## Settings
| Setting | Meaning |
|---|---|
| `persona` | The name your house assistant uses (default **Watson**) |
| `site_id` | Short name for this home, given to you (e.g. `smith-family`) |
| `site_key` | The secret key for this home, given to you |
| `central_url` | Address of the Housekeeper service |
| `healthchecks_url` | Optional independent check-in URL |
| `watch` | Optional, set by your support person: entities that must not stay in a bad state, as JSON, e.g. `{"entity": "sensor.alarm_status", "bad": ["Fault"], "for_min": 60}` |
