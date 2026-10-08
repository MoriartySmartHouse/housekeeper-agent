# Housekeeper Agent

Once an hour this App checks the health of your home — backups, repairs, waiting updates, devices that
have stopped reporting, low batteries — and sends a short report card to the Housekeeper service run by the
person who looks after your system. It shows the result in your Home Assistant as a status from **Watson**, your
house assistant (`sensor.housekeeper_status`; you can rename Watson in the settings).

## What it can see, and what leaves your home
- To read your system it uses the access Home Assistant gives Apps that talk to it. **Be aware: that access
  is administrator-level — it could read *and change* things.** This App's code only reads, except for
  writing its own status sensor; an automatic test fails the build if any other write is ever added. The
  code is public in the repository above.
- **What leaves your home:** check results only — e.g. "backup 8 h old", "Front door lock battery 18 %".
  That includes device names, the names of waiting updates (which show what apps you have installed), repair
  ids, and the count — not the text — of your notifications. **Never** camera images, locations,
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

`central_url` must be `https://`; plain `http://` is accepted only for a private or Tailscale address.
The App refuses to start (and says why in its log) if a required setting is blank or invalid.
| `watch_entities` | Optional, set by your support person: entities that must not stay in a bad state — entity, bad states (comma-separated), minutes |
| `watch` | Older JSON format of the same, still accepted |

## Good to know
- A Zigbee device is flagged as silent after it has been unavailable for 24 h. Zigbee2MQTT itself waits
  about 25 h before marking a battery device unavailable, so in practice a dead sensor shows after ~2 days.
  Devices that never go unavailable but stop sending anything are flagged after 48 h.
