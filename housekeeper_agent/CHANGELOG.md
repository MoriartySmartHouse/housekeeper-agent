# Changelog

## 0.1.3
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
