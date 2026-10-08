# Changelog

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
