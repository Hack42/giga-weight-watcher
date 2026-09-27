# Changelog

All notable changes to this project are documented here.

## 1.1.0 - 2026-09-27

- Add a udev rule providing the stable `/dev/ultraship-u2` device name.
- Detect `kg`, `g`, `oz`, and `lb` modes from continuous serial packets.
- Convert every supported unit to grams before averaging and publishing.

## 1.0.0 - 2026-09-16

- Port the original reader to Python 3 and bytes-safe serial handling.
- Add support for continuous 14-byte ASCII frames.
- Add robust packet re-synchronization.
- Add MQTT publishing with retained state and availability.
- Add Home Assistant MQTT Discovery.
- Add one-second sample averaging and five-minute heartbeats.
- Add automatic USB and MQTT reconnection.
- Add an optional, re-arming Moonraker low-weight pause monitor.
- Add environment configuration, a systemd user service, packaging, and tests.
