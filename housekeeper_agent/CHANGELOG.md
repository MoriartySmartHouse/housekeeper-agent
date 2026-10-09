# Changelog

## 0.1.5
- For **safer updates**: the update check now sends the installed and offered version of each waiting update, and
  the list of integrations your Home Assistant has loaded (their short names only, e.g. `mqtt`, `zha`). The
  Housekeeper service matches these against each Home Assistant release's breaking changes, so you hear *before*
  updating which changes touch your home. Read with one more read-only call (`get_config`).
- New check: **automations that stopped firing.** Watson learns, in your home, how often each automation usually
  runs; one that has gone quiet for 4x its usual gap (at least 2 days) is reported, as is one Home Assistant could
  not load. Rarely-running automations such as leak alarms are never flagged. Trigger times stay in your home —
  only the automation's name and "silent N days, usually every X" leave. Needs a few runs of each automation to learn;
  ignore any with the new *Automations to ignore* setting.

## 0.1.4
- New check: **devices that keep dropping off** — a plug, light or switch that went unavailable 3 or more
  times in 24 hours (e.g. weak WiFi). To see the short drop-offs it reads the last 24 h of on/off history of
  one such entity per device, on your Home Assistant; only the device name and the number of drop-offs leave.
- Watson's status now carries a weekly **note** from the Housekeeper service: what got sorted out this week and
  any battery that will run out soon. A ready-made dashboard card is in the documentation.
- Battery check: reports the level of every battery (not only the low ones), so the service can forecast
  when a battery will run out.
- Built on Python 3.14.

## 0.1.3
- Home Assistant now downloads a prebuilt image for this App instead of building it on your machine:
  faster updates, less wear on the disk, no build failures. Your settings are kept.
- The App now runs under its own AppArmor profile (Python, its own code and `/data`, outbound network
  only). The security rating shown in Home Assistant rises from 7 to 8
  (from 5 to 6 on Home Assistant systems older than September 2026).
- Stops cleanly when the App is stopped, restarted or updated (it used to be force-killed after 10 seconds,
  leaving the App shown in an error state).

## 0.1.2
- Silent devices: the "Zigbee devices without a last-seen sensor" list now shows only Zigbee2MQTT devices,
  not the Zigbee2MQTT bridge or other MQTT devices (e.g. BirdNET-Go).
- Silent devices: devices that are unavailable but not yet past the silent threshold are now named, with
  since when, in the report details.

## 0.1.1
- Silent devices: no longer fooled by Home Assistant restarts. The agent now remembers when a device went
  quiet, and uses Zigbee2MQTT's own "last seen" time where that sensor is enabled.
- Settings form: plain-English names and help text for every setting.
- New "Things to watch" fields (entity, bad states, minutes) instead of typing JSON. The old format still works.

## 0.1.0
- First release: backups, repairs, notifications, updates, silent devices, batteries, Supervisor health,
  watched entities. Reports hourly; shows status as your house assistant (default "Watson").
