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
  That includes device names; the names and versions of waiting updates (which show what apps you have
  installed); the short names of the integrations your Home Assistant has loaded (e.g. `mqtt` — used to warn
  you about breaking changes before you update); repair ids; the level of each battery; the names of plugs and
  lights that keep dropping off the network; the names of automations that stopped running, with how long and
  how often they usually run; the names of switches that are on but drawing no power; free disk space; and the count — not the text — of your notifications. **Never** camera images,
  locations, history, or your passwords. (To spot drop-offs the App reads the last 24 h of on/off history of
  plugs, lights and switches; to spot stopped automations it remembers their recent run times. Both stay in your
  home.)
- To stop it at any time: stop or uninstall this App.

## Security
- **Prebuilt image.** Home Assistant downloads a ready-made image for this App
  (`ghcr.io/moriartysmarthouse/housekeeper-agent`) instead of building it on your machine. It is built
  from this repository's public code by GitHub Actions, only when a version is tagged; the build log
  is public on the repository's *Actions* tab.
- **AppArmor profile.** The App ships its own AppArmor profile (`apparmor.txt`): it can run Python, read
  its own code, read and write its own `/data` folder, and make outbound network connections. It cannot
  start a shell or other programs, write anywhere else, or use any special Linux privileges.
- **Security rating.** Home Assistant shows **8** (the highest; 6 on Home Assistant systems older than
  September 2026) once the App is installed or updated: it opens no ports, uses neither host networking
  nor elevated Supervisor roles, and has its own AppArmor profile. The rating does **not** measure the
  Home Assistant API access described above, which is the access that matters most here.
- **Not signed.** Images are not signed: Home Assistant does not currently check image signatures, so
  a signature would not protect you. Trust comes from the public code and public build.

## Settings
| Setting | Meaning |
|---|---|
| `persona` | The name your house assistant uses (default **Watson**) |
| `site_id` | Short name for this home, given to you (e.g. `smith-family`) |
| `site_key` | The secret key for this home, given to you |
| `central_url` | Address of the Housekeeper service |
| `healthchecks_url` | Optional independent check-in URL |
| `silent_exceptions` | Optional: device names never to report as silent (e.g. something unplugged on purpose) |
| `zero_power_hours` / `zero_power_exceptions` | When to report a switch that is on at 0 W (default 6 h); switches allowed to sit at 0 W (e.g. a plug whose lamp is off at the lamp) |
| `automation_exceptions` | Optional: automation names never to report as "stopped running" (e.g. a seasonal one) |
| `watch_entities` | Optional, set by your support person: entities that must not stay in a bad state — entity, bad states (comma-separated), minutes |
| `watch` | Older JSON format of the same, still accepted |

`central_url` must be `https://`; plain `http://` is accepted only for a private or Tailscale address.
The App refuses to start (and says why in its log) if a required setting is blank or invalid.

## Show Watson on a dashboard
Add a **Markdown** card (Edit dashboard → Add card → Markdown) with this content:

```
## {{ state_attr('sensor.housekeeper_status', 'friendly_name') or 'Watson' }}
{% set m = state_attr('sensor.housekeeper_status', 'messages') or [] %}
{% if m %}{% for x in m %}- {{ x }}
{% endfor %}{% else %}Everything looks good.{% endif %}
{% set n = state_attr('sensor.housekeeper_status', 'note') %}{% if n %}

*{{ n }}*{% endif %}

<sub>Checked {{ relative_time(as_datetime(state_attr('sensor.housekeeper_status', 'checked_at'))) }} ago</sub>
```

## Good to know
- A Zigbee device is flagged as silent after it has been unavailable for 24 h. Zigbee2MQTT itself waits
  about 25 h before marking a battery device unavailable, so in practice a dead sensor shows after ~2 days.
  Devices that never go unavailable but stop sending anything are flagged after 48 h.
