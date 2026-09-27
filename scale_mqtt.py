#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Publish an UltraShip U-2 scale to MQTT and pause a low-weight print."""

import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
import os
import signal
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ultraship_u2v2 import UltrashipU2v2


LOG = logging.getLogger('scale_mqtt')
GRAMS_PER_UNIT = {
    'g': Decimal('1'),
    'kg': Decimal('1000'),
    'oz': Decimal('28.349523125'),
    'lb': Decimal('453.59237'),
}


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def reading_to_grams(reading, input_unit):
    """Convert a scale reading to grams without floating-point rounding."""
    value = Decimal(reading.strip())
    try:
        factor = GRAMS_PER_UNIT[input_unit.lower()]
    except KeyError:
        raise ValueError('unsupported input unit: {}'.format(input_unit))
    return value * factor


def format_grams(grams):
    """Return a non-exponential MQTT number payload."""
    if grams == grams.to_integral_value():
        return str(int(grams))
    return format(grams.normalize(), 'f')


def should_publish(weight, previous_weight, now, previous_time, interval):
    return (
        previous_weight is None
        or weight != previous_weight
        or now - previous_time >= interval
    )


class SampleAverager:
    """Produce one rounded average for each interval containing samples."""

    def __init__(self, interval=1, resolution=Decimal('0.1')):
        self.interval = interval
        self.resolution = Decimal(str(resolution))
        self.window_start = None
        self.total = Decimal(0)
        self.count = 0

    def add(self, value, now=None):
        if now is None:
            now = time.monotonic()

        if self.window_start is None:
            self.window_start = now

        if now - self.window_start >= self.interval and self.count:
            average = (self.total / self.count).quantize(
                self.resolution, rounding=ROUND_HALF_UP
            )
            self.window_start = now
            self.total = value
            self.count = 1
            return average

        self.total += value
        self.count += 1
        return None


def build_discovery_config(state_topic, availability_topic, device_id, name):
    return {
        'name': '{} weight'.format(name),
        'unique_id': '{}_weight'.format(device_id),
        'state_topic': state_topic,
        'availability_topic': availability_topic,
        'payload_available': 'online',
        'payload_not_available': 'offline',
        'unit_of_measurement': 'g',
        'device_class': 'weight',
        'state_class': 'measurement',
        'value_template': '{{ value | float }}',
        'device': {
            'identifiers': [device_id],
            'name': name,
            'manufacturer': 'My Weigh',
            'model': 'UltraShip U-2',
        },
    }


class MqttPublisher:
    def __init__(self, args):
        import paho.mqtt.client as mqtt

        self.host = args.mqtt_host
        self.port = args.mqtt_port
        self.state_topic = args.mqtt_topic.rstrip('/') + '/weight'
        self.availability_topic = args.mqtt_topic.rstrip('/') + '/availability'
        self.discovery_topic = '{}/sensor/{}/weight/config'.format(
            args.discovery_prefix.rstrip('/'), args.device_id
        )
        self.discovery_payload = json.dumps(
            build_discovery_config(
                self.state_topic,
                self.availability_topic,
                args.device_id,
                args.device_name,
            ),
            separators=(',', ':'),
            sort_keys=True,
        )
        self._connected = threading.Event()
        self._lock = threading.RLock()
        self._scale_available = False
        self._latest_weight = None

        self.client = mqtt.Client(client_id=args.mqtt_client_id, clean_session=True)
        if args.mqtt_username:
            self.client.username_pw_set(args.mqtt_username, args.mqtt_password)
        if args.mqtt_tls:
            self.client.tls_set(ca_certs=args.mqtt_ca_cert)
        self.client.will_set(
            self.availability_topic, 'offline', qos=1, retain=True
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def start(self):
        LOG.info('Connecting to MQTT at %s:%s', self.host, self.port)
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self):
        if self._connected.is_set():
            self.client.publish(
                self.availability_topic, 'offline', qos=1, retain=True
            ).wait_for_publish(timeout=2)
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            LOG.error('MQTT connection refused (rc=%s)', rc)
            return

        LOG.info('MQTT connected')
        self._connected.set()
        with self._lock:
            client.publish(
                self.discovery_topic,
                self.discovery_payload,
                qos=1,
                retain=True,
            )
            client.publish(
                self.availability_topic,
                'online' if self._scale_available else 'offline',
                qos=1,
                retain=True,
            )
            if self._latest_weight is not None:
                client.publish(
                    self.state_topic,
                    format_grams(self._latest_weight),
                    qos=1,
                    retain=True,
                )

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc:
            LOG.warning('MQTT disconnected (rc=%s); reconnecting', rc)
        else:
            LOG.info('MQTT connection closed')

    def set_scale_available(self, available):
        with self._lock:
            changed = available != self._scale_available
            self._scale_available = available
            if changed and self._connected.is_set():
                self.client.publish(
                    self.availability_topic,
                    'online' if available else 'offline',
                    qos=1,
                    retain=True,
                )

    def publish_weight(self, grams):
        with self._lock:
            self._latest_weight = grams
            if self._connected.is_set():
                self.client.publish(
                    self.state_topic,
                    format_grams(grams),
                    qos=1,
                    retain=True,
                )


class PauseMonitor:
    """Pause below the low threshold and re-arm above the high threshold."""

    def __init__(self, start_threshold, pause_threshold, pause_action,
                 retry_interval=10):
        self.start_threshold = Decimal(str(start_threshold))
        self.pause_threshold = Decimal(str(pause_threshold))
        self.pause_action = pause_action
        self.retry_interval = retry_interval
        self.initial_weight = None
        self.armed = False
        self.triggered = False
        self.next_attempt = 0

    def observe(self, grams, now=None):
        if now is None:
            now = time.monotonic()

        if self.initial_weight is None:
            self.initial_weight = grams
            if grams <= self.start_threshold:
                LOG.info(
                    'Pause monitor not armed: initial weight %s g <= %s g',
                    format_grams(grams), format_grams(self.start_threshold)
                )

        if not self.armed:
            if grams > self.start_threshold:
                self.armed = True
                self.triggered = False
                self.next_attempt = 0
                LOG.warning(
                    'Pause monitor armed: weight %s g > %s g',
                    format_grams(grams), format_grams(self.start_threshold)
                )
            return False

        if (
                grams >= self.pause_threshold
                or now < self.next_attempt):
            return False

        LOG.warning(
            'Weight %s g < %s g: sending PAUSE to the print process',
            format_grams(grams), format_grams(self.pause_threshold)
        )
        try:
            self.pause_action()
        except Exception:
            self.next_attempt = now + self.retry_interval
            LOG.exception(
                'PAUSE failed; retrying in %s seconds',
                self.retry_interval,
            )
            return False

        self.armed = False
        self.triggered = True
        LOG.warning(
            'PAUSE sent; waiting for weight > %s g before re-arming',
            format_grams(self.start_threshold),
        )
        return True


def pause_moonraker(url, timeout=10):
    request = Request(url, data=b'', method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode('utf-8')
    except (HTTPError, URLError, OSError) as error:
        raise RuntimeError('Moonraker PAUSE failed: {}'.format(error))

    if payload:
        result = json.loads(payload)
        if 'error' in result:
            raise RuntimeError('Moonraker PAUSE error: {}'.format(result['error']))


def run(args, stop_event):
    import serial

    publisher = MqttPublisher(args)
    publisher.start()
    monitor = PauseMonitor(
        args.start_threshold_grams,
        args.pause_threshold_grams,
        lambda: pause_moonraker(args.pause_url),
        retry_interval=args.pause_retry_interval,
    )
    previous_weight = None
    previous_publish_time = 0

    try:
        while not stop_event.is_set():
            port = None
            try:
                LOG.info('Opening scale at %s', args.device)
                port = serial.Serial(args.device, 9600, timeout=2)
                scale = UltrashipU2v2(port)
                averager = SampleAverager(
                    args.average_interval, args.average_resolution_grams
                )
                first_reading = True

                while not stop_event.is_set():
                    raw_reading, detected_unit = scale.read_measurement()
                    input_unit = detected_unit or args.input_unit
                    grams = reading_to_grams(raw_reading, input_unit)
                    now = time.monotonic()

                    if first_reading:
                        LOG.info(
                            'Scale connected; first reading %s %s (%s g)',
                            raw_reading,
                            input_unit,
                            format_grams(grams),
                        )
                        publisher.set_scale_available(True)
                        first_reading = False

                    averaged_grams = averager.add(grams, now)
                    if averaged_grams is None:
                        continue

                    if should_publish(
                            averaged_grams,
                            previous_weight,
                            now,
                            previous_publish_time,
                            args.publish_interval):
                        publisher.publish_weight(averaged_grams)
                        previous_weight = averaged_grams
                        previous_publish_time = now
                        LOG.info(
                            'Published one-second average: %s g',
                            format_grams(averaged_grams),
                        )

                    if not args.disable_pause:
                        monitor.observe(averaged_grams, now)

            except (serial.SerialException, OSError) as error:
                publisher.set_scale_available(False)
                LOG.warning(
                    'USB scale unavailable (%s); retrying in %s seconds',
                    error,
                    args.usb_retry_interval,
                )
                stop_event.wait(args.usb_retry_interval)
            except (InvalidOperation, ValueError) as error:
                LOG.warning('Ignoring invalid scale reading: %s', error)
            finally:
                if port is not None:
                    port.close()
    finally:
        publisher.set_scale_available(False)
        publisher.stop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Publish an UltraShip U-2 scale to MQTT and Home Assistant.'
    )
    parser.add_argument(
        '--device', default=os.getenv('SCALE_DEVICE', '/dev/ultraship-u2')
    )
    parser.add_argument(
        '--input-unit', choices=('kg', 'g', 'oz', 'lb'),
        default=os.getenv('INPUT_UNIT', 'kg'),
        help='fallback unit when the serial packet has no unit (default: kg)',
    )
    parser.add_argument(
        '--mqtt-host', default=os.getenv('MQTT_HOST', 'localhost')
    )
    parser.add_argument('--mqtt-port', type=int, default=os.getenv('MQTT_PORT', '1883'))
    parser.add_argument(
        '--mqtt-topic', default=os.getenv('MQTT_TOPIC', 'ultraship-u2/scale')
    )
    parser.add_argument(
        '--mqtt-client-id',
        default=os.getenv('MQTT_CLIENT_ID', 'ultraship-u2-scale'),
    )
    parser.add_argument('--mqtt-username', default=os.getenv('MQTT_USERNAME'))
    parser.add_argument('--mqtt-password', default=os.getenv('MQTT_PASSWORD'))
    parser.add_argument(
        '--mqtt-tls', action='store_true', default=env_bool('MQTT_TLS')
    )
    parser.add_argument('--mqtt-ca-cert', default=os.getenv('MQTT_CA_CERT'))
    parser.add_argument(
        '--discovery-prefix',
        default=os.getenv('DISCOVERY_PREFIX', 'homeassistant'),
    )
    parser.add_argument(
        '--device-id', default=os.getenv('DEVICE_ID', 'ultraship_u2_scale')
    )
    parser.add_argument(
        '--device-name', default=os.getenv('DEVICE_NAME', 'UltraShip U-2 scale')
    )
    parser.add_argument(
        '--publish-interval', type=float, default=os.getenv('PUBLISH_INTERVAL', '300')
    )
    parser.add_argument(
        '--average-interval',
        type=float,
        default=os.getenv('AVERAGE_INTERVAL', '1'),
    )
    parser.add_argument(
        '--average-resolution-grams',
        type=Decimal,
        default=os.getenv('AVERAGE_RESOLUTION_GRAMS', '0.1'),
    )
    parser.add_argument(
        '--usb-retry-interval',
        type=float,
        default=os.getenv('USB_RETRY_INTERVAL', '5'),
    )
    parser.add_argument(
        '--start-threshold-grams',
        type=Decimal,
        default=os.getenv('START_THRESHOLD_GRAMS', '1000'),
    )
    parser.add_argument(
        '--pause-threshold-grams',
        type=Decimal,
        default=os.getenv('PAUSE_THRESHOLD_GRAMS', '500'),
    )
    parser.add_argument(
        '--pause-retry-interval',
        type=float,
        default=os.getenv('PAUSE_RETRY_INTERVAL', '10'),
    )
    parser.add_argument(
        '--pause-url',
        default=os.getenv(
            'PAUSE_URL', 'http://127.0.0.1:7125/printer/print/pause'
        ),
    )
    parser.add_argument(
        '--disable-pause', action='store_true', default=env_bool('DISABLE_PAUSE')
    )
    parser.add_argument(
        '--log-level',
        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'),
        default=os.getenv('LOG_LEVEL', 'INFO'),
    )
    args = parser.parse_args(argv)

    if args.start_threshold_grams <= args.pause_threshold_grams:
        parser.error('--start-threshold-grams must exceed --pause-threshold-grams')
    for name in (
            'publish_interval',
            'average_interval',
            'average_resolution_grams',
            'usb_retry_interval',
            'pause_retry_interval'):
        if getattr(args, name) <= 0:
            parser.error('--{} must be greater than zero'.format(name.replace('_', '-')))
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(message)s',
    )
    stop_event = threading.Event()

    def stop(signum, frame):
        LOG.info('Stop signal received')
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        run(args, stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    except Exception:
        LOG.exception('Fatal error')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
