# Changelog

## 0.1.1
- Silent devices: no longer fooled by Home Assistant restarts. The agent now remembers when a device went
  quiet, and uses Zigbee2MQTT's own "last seen" time where that sensor is enabled.
- Settings form: plain-English names and help text for every setting.
- New "Things to watch" fields (entity, bad states, minutes) instead of typing JSON. The old format still works.

## 0.1.0
- First release: backups, repairs, notifications, updates, silent devices, batteries, Supervisor health,
  watched entities. Reports hourly; shows status as your house assistant (default "Watson").
