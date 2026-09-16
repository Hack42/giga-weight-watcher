# UltraShip U-2 MQTT Bridge

[![License: BSD-2-Clause](https://img.shields.io/badge/license-BSD--2--Clause-blue.svg)](LICENSE)

This project solves a practical problem with large 5 kg filament spools: near
the end of a spool, the remaining filament can hang up instead of feeding
cleanly into the printer. By continuously weighing the spool, the bridge can
detect that it is nearly empty and pause the print before this happens.

A small, dependency-light Linux service that reads a
[My Weigh UltraShip U-2](https://digital-scales-company.co.uk/commercial-postal-scales/my-weigh-ultraship-u2-2-line-display-postal-scale-60lb-27kg)
over USB serial, publishes averaged weights to MQTT, creates a Home Assistant
sensor through MQTT Discovery, and can pause a Klipper/Moonraker print when the
remaining weight becomes too low.

The project runs on Python 3.7 and newer, including the older Python shipped on
several Elegoo printer controllers.

## Features

- Supports the legacy encrypted/checksummed U-2 v2 protocol.
- Supports the plain 14-byte ASCII protocol used in continuous mode.
- Re-synchronizes after truncated or malformed serial data.
- Reopens the USB device automatically after disconnects.
- Reconnects to MQTT automatically and uses retained state and availability.
- Publishes one-second averages, not every raw scale sample.
- Republishes an unchanged value periodically as a heartbeat.
- Registers a `weight` sensor through Home Assistant MQTT Discovery.
- Optionally pauses a Moonraker print below a configurable low threshold.
- Re-arms only after the weight passes a separate refill threshold.
- Includes a reboot-persistent systemd user service.

## Requirements

- Linux
- Python 3.7 or newer
- An UltraShip U-2 USB-serial model
- An MQTT 3.1.1 broker
- Optional: Moonraker for automatic print pausing

Known USB-serial revisions use PL2303 or CH340 adapters and normally appear as
`/dev/ttyUSB0`. The older USB HID revision is not supported.

## Installation

Clone or download this repository, then install it for the current user:

```console
git clone https://github.com/Hack42/giga-weight-watcher.git
cd giga-weight-watcher
python3 -m pip install --user .
```

Make sure `~/.local/bin` is in `PATH`. Two commands are installed:

```console
ultraship-u2-read --help
ultraship-u2-mqtt --help
```

If access to the serial device is denied, add the service user to `dialout`,
then log out and back in:

```console
sudo usermod -aG dialout "$USER"
```

## Test the scale

Read the raw display value before setting up MQTT:

```console
ultraship-u2-read /dev/ttyUSB0
```

In continuous mode the scale sends data automatically. In on-demand mode,
press `SEND` for each reading.

## Configure MQTT

Copy the example configuration:

```console
mkdir -p ~/.config
cp examples/ultraship-u2-mqtt.env ~/.config/ultraship-u2-mqtt.env
```

Edit at least `MQTT_HOST`. The most relevant settings are:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `SCALE_DEVICE` | `/dev/ttyUSB0` | USB serial device |
| `INPUT_UNIT` | `kg` | Unit shown by the scale (`kg` or `g`) |
| `MQTT_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `ultraship-u2/scale` | Base topic |
| `MQTT_USERNAME` | empty | Optional broker username |
| `MQTT_PASSWORD` | empty | Optional broker password |
| `MQTT_TLS` | `false` | Enable TLS certificate verification |
| `MQTT_CA_CERT` | empty | Optional custom CA certificate |
| `AVERAGE_INTERVAL` | `1` | Averaging window in seconds |
| `AVERAGE_RESOLUTION_GRAMS` | `0.1` | Published weight resolution |
| `PUBLISH_INTERVAL` | `300` | Heartbeat interval in seconds |
| `USB_RETRY_INTERVAL` | `5` | Delay before reopening USB |
| `START_THRESHOLD_GRAMS` | `1000` | Weight required to arm PAUSE |
| `PAUSE_THRESHOLD_GRAMS` | `500` | Weight below which PAUSE is sent |
| `PAUSE_URL` | Moonraker localhost URL | Print-pause endpoint |
| `DISABLE_PAUSE` | `false` | Disable all automatic pause calls |

Command-line arguments override the built-in defaults. Environment variables
are convenient for the systemd service.

## Safe first run

Test MQTT without allowing print control:

```console
set -a
. ~/.config/ultraship-u2-mqtt.env
set +a
ultraship-u2-mqtt --disable-pause
```

Stop with Ctrl-C. The bridge publishes these topics by default:

```text
ultraship-u2/scale/weight
ultraship-u2/scale/availability
homeassistant/sensor/ultraship_u2_scale/weight/config
```

The weight and discovery configuration are retained. Availability becomes
`offline` after a clean stop, USB failure, or unexpected MQTT disconnect via
the MQTT Last Will.

## Home Assistant

MQTT Discovery is enabled by default in Home Assistant. Once Home Assistant is
connected to the same broker, the bridge creates an **UltraShip U-2 scale
weight** sensor measured in grams with `measurement` state class.

No YAML configuration is required. Change `DISCOVERY_PREFIX` only when Home
Assistant uses a non-default MQTT Discovery prefix.

## Automatic print pause

The default Moonraker endpoint is:

```text
http://127.0.0.1:7125/printer/print/pause
```

The pause monitor is a hysteresis state machine based on one-second averages:

```text
weight > START_THRESHOLD_GRAMS  -> armed
weight < PAUSE_THRESHOLD_GRAMS  -> send PAUSE, then disarm
weight > START_THRESHOLD_GRAMS  -> armed again
```

With the defaults, a refill over 1000 g arms the monitor and a later drop below
500 g pauses the print. Failed pause requests are retried with a backoff.

> [!WARNING]
> Automatic pausing is a convenience feature, not a safety system. Verify the
> scale, thresholds, networking, and Moonraker endpoint before relying on it.
> Start with `DISABLE_PAUSE=true` or `--disable-pause`.

## Run as a systemd user service

After installing the package and configuration:

```console
mkdir -p ~/.config/systemd/user
cp systemd/ultraship-u2-mqtt.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ultraship-u2-mqtt.service
```

Keep the user service running without an interactive login:

```console
loginctl enable-linger "$USER"
```

Some systems require `sudo` for that last command. Check the service with:

```console
systemctl --user status ultraship-u2-mqtt.service
```

After changing the environment file:

```console
systemctl --user restart ultraship-u2-mqtt.service
```

## Protocol notes

Continuous mode frames observed in the field are 14 bytes long:

```text
02 0b 44 20 20 20 30 2e 34 34 36 4b 4b 03
STX 11          D   0.446KK             ETX
```

The parser also retains compatibility with the older 14-byte encrypted and
checksummed protocol. See [docs/protocol.md](docs/protocol.md).

## Development

Run the test suite without hardware:

```console
python3 -m unittest -v
```

The tests cover both protocols, stream re-synchronization, unit conversion,
one-second averaging, MQTT discovery, heartbeat behavior, pause retries, and
multiple arm/pause/refill cycles.

## License and attribution

This project is distributed under the [BSD 2-Clause License](LICENSE).

The original USB-serial decoder was written by Timothy Twillman in 2012. The
Python 3 port and the MQTT, Home Assistant, Moonraker, continuous-mode, service,
test, and documentation work were added by later contributors. See [NOTICE](NOTICE).
