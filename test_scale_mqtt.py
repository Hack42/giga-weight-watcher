# SPDX-License-Identifier: BSD-2-Clause
import logging
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from scale_mqtt import (
    PauseMonitor,
    SampleAverager,
    build_discovery_config,
    format_grams,
    env_bool,
    parse_args,
    reading_to_grams,
    should_publish,
)


logging.disable(logging.CRITICAL)


class ConversionTest(unittest.TestCase):
    def test_kg_to_grams(self):
        self.assertEqual(reading_to_grams('0.448', 'kg'), Decimal('448'))

    def test_negative_kg_to_grams(self):
        self.assertEqual(reading_to_grams('-0.125', 'kg'), Decimal('-125'))

    def test_gram_input(self):
        self.assertEqual(reading_to_grams('123.5', 'g'), Decimal('123.5'))

    def test_payload_format(self):
        self.assertEqual(format_grams(Decimal('448.000')), '448')
        self.assertEqual(format_grams(Decimal('448.5')), '448.5')


class PublishingTest(unittest.TestCase):
    def test_boolean_environment_setting(self):
        with patch.dict(os.environ, {'MQTT_TLS': 'yes'}):
            self.assertTrue(env_bool('MQTT_TLS'))

    def test_one_second_average(self):
        averager = SampleAverager(interval=1, resolution=Decimal('0.1'))
        self.assertIsNone(averager.add(Decimal('100'), now=0))
        self.assertIsNone(averager.add(Decimal('200'), now=0.5))
        self.assertEqual(averager.add(Decimal('300'), now=1), Decimal('150.0'))
        self.assertEqual(averager.add(Decimal('400'), now=2), Decimal('300.0'))

    def test_average_rounds_to_tenth_of_gram(self):
        averager = SampleAverager(interval=1, resolution=Decimal('0.1'))
        averager.add(Decimal('100'), now=0)
        averager.add(Decimal('101'), now=0.4)
        averager.add(Decimal('101'), now=0.8)
        self.assertEqual(averager.add(Decimal('200'), now=1), Decimal('100.7'))

    def test_first_and_changed_values_publish(self):
        self.assertTrue(should_publish(Decimal(1), None, 10, 0, 300))
        self.assertTrue(should_publish(Decimal(2), Decimal(1), 20, 10, 300))

    def test_unchanged_value_gets_five_minute_heartbeat(self):
        weight = Decimal(1)
        self.assertFalse(should_publish(weight, weight, 309, 10, 300))
        self.assertTrue(should_publish(weight, weight, 310, 10, 300))

    def test_home_assistant_discovery(self):
        config = build_discovery_config('scale/weight', 'scale/availability', 'scale_1', 'Scale')
        self.assertEqual(config['device_class'], 'weight')
        self.assertEqual(config['state_class'], 'measurement')
        self.assertEqual(config['unit_of_measurement'], 'g')
        self.assertEqual(config['state_topic'], 'scale/weight')
        self.assertEqual(config['availability_topic'], 'scale/availability')

    def test_thresholds_can_be_configured_with_environment(self):
        with patch.dict(os.environ, {
                'START_THRESHOLD_GRAMS': '1500',
                'PAUSE_THRESHOLD_GRAMS': '250',
                'PUBLISH_INTERVAL': '60',
                'AVERAGE_INTERVAL': '2',
        }):
            args = parse_args([])
        self.assertEqual(args.start_threshold_grams, Decimal('1500'))
        self.assertEqual(args.pause_threshold_grams, Decimal('250'))
        self.assertEqual(args.publish_interval, 60)
        self.assertEqual(args.average_interval, 2)


class PauseMonitorTest(unittest.TestCase):
    def test_arms_pauses_and_rearms_after_refill(self):
        calls = []
        monitor = PauseMonitor(1000, 500, lambda: calls.append('PAUSE'))

        self.assertFalse(monitor.observe(Decimal('1200'), now=0))
        self.assertTrue(monitor.armed)
        self.assertFalse(monitor.observe(Decimal('500'), now=1))
        self.assertTrue(monitor.observe(Decimal('499'), now=2))
        self.assertFalse(monitor.observe(Decimal('400'), now=3))
        self.assertFalse(monitor.armed)

        self.assertFalse(monitor.observe(Decimal('1001'), now=4))
        self.assertTrue(monitor.armed)
        self.assertTrue(monitor.observe(Decimal('499'), now=5))
        self.assertEqual(calls, ['PAUSE', 'PAUSE'])

    def test_does_not_arm_at_or_below_start_threshold(self):
        calls = []
        monitor = PauseMonitor(1000, 500, lambda: calls.append('PAUSE'))
        monitor.observe(Decimal('1000'), now=0)
        monitor.observe(Decimal('400'), now=1)
        self.assertFalse(monitor.armed)
        self.assertEqual(calls, [])

    def test_can_arm_later_when_started_below_threshold(self):
        calls = []
        monitor = PauseMonitor(1000, 500, lambda: calls.append('PAUSE'))
        monitor.observe(Decimal('400'), now=0)
        monitor.observe(Decimal('1200'), now=1)
        monitor.observe(Decimal('400'), now=2)
        self.assertEqual(calls, ['PAUSE'])

    def test_failed_pause_is_retried_after_backoff(self):
        calls = []

        def fail():
            calls.append('attempt')
            raise RuntimeError('not printing')

        monitor = PauseMonitor(1000, 500, fail, retry_interval=10)
        monitor.observe(Decimal('1200'), now=0)
        monitor.observe(Decimal('400'), now=1)
        monitor.observe(Decimal('400'), now=5)
        monitor.observe(Decimal('400'), now=11)
        self.assertEqual(calls, ['attempt', 'attempt'])
        self.assertTrue(monitor.armed)


if __name__ == '__main__':
    unittest.main()
