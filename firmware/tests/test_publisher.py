"""Tests for lib/publisher.py with a fake umqtt client."""

import unittest
from unittest import mock

from fakes import FakeMQTTClient

import publisher


def factory(**flags):
    return lambda cid, host, port: FakeMQTTClient(cid, host, port, **flags)


class PublisherTest(unittest.TestCase):
    def setUp(self):
        FakeMQTTClient.instances = []
        self.quiet = mock.patch("builtins.print")
        self.quiet.start()

    def tearDown(self):
        self.quiet.stop()

    def test_connects_on_first_publish_with_qos_1(self):
        pub = publisher.Publisher("test-01", "broker", 1883, factory())
        self.assertTrue(pub.publish("noise/tepic/test-01/clip", b"x"))
        client = FakeMQTTClient.instances[0]
        self.assertEqual(client.args, ("test-01", "broker", 1883))
        self.assertEqual(client.published, [("noise/tepic/test-01/clip", b"x", 1)])

    def test_publish_failure_never_raises_and_reconnects(self):
        pub = publisher.Publisher("test-01", "broker", 1883, factory(fail_publish=True))
        self.assertFalse(pub.publish("t", b"x"))
        self.assertFalse(pub.connected)
        self.assertFalse(pub.publish("t", b"x"))
        self.assertEqual(len(FakeMQTTClient.instances), 2)  # reconnected right away

    def test_dead_broker_is_retried_only_every_n_publishes(self):
        pub = publisher.Publisher("test-01", "broker", 1883, factory(fail_connect=True))
        for _ in range(publisher.RECONNECT_EVERY * 2):
            self.assertFalse(pub.publish("t", b"x"))
        self.assertEqual(len(FakeMQTTClient.instances), 2)


if __name__ == "__main__":
    unittest.main()
