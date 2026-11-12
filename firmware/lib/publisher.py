"""MQTT publishing for firmware v2 (contracts/mqtt.md).

Thin wrapper around umqtt.simple: QoS 1 publishes that never raise, a socket
timeout on every publish (umqtt.simple waits for PUBACK without one), and a
reconnect that is attempted at most once every RECONNECT_EVERY failures so a
dead broker does not stall the capture loop.
"""

try:
    from umqtt.simple import MQTTClient
except ImportError:  # CPython, for the tests on the PC
    MQTTClient = None

TIMEOUT_S = 10
RECONNECT_EVERY = 10


class Publisher:
    """Keeps one MQTT connection to the broker.

    Args:
        client_id: MQTT client id (the NODE_ID).
        host: broker host or IP.
        port: broker port.
        client_factory: callable(client_id, host, port) returning an object
            with the umqtt.simple API; defaults to umqtt.simple.MQTTClient.
    """

    def __init__(self, client_id, host, port=1883, client_factory=None, timeout_s=TIMEOUT_S):
        self.client_id = client_id
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.factory = client_factory or (
            lambda cid, h, p: MQTTClient(cid, h, port=p, keepalive=60))
        self.client = None
        self.failures = 0

    @property
    def connected(self):
        return self.client is not None

    def connect(self):
        """Open the connection.

        Returns:
            True if connected, False otherwise (never raises).
        """
        self.close()
        try:
            client = self.factory(self.client_id, self.host, self.port)
            client.connect()
            self.client = client
            self.failures = 0
            return True
        except Exception as exc:  # OSError, MQTTException
            print("mqtt: connect failed:", repr(exc))
            self.client = None
            return False

    def close(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.client = None

    def publish(self, topic, payload):
        """Publish with QoS 1 and wait for PUBACK.

        If there is no connection, a reconnect is tried on the first failure
        and then once every RECONNECT_EVERY calls.

        Returns:
            True if the broker acknowledged the message, False otherwise.
        """
        if self.client is None:
            self.failures += 1
            if self.failures % RECONNECT_EVERY != 1 or not self.connect():
                return False
        try:
            sock = getattr(self.client, "sock", None)
            if sock is not None:
                # umqtt.simple resets the socket to blocking after each
                # message, so the timeout must be set again every time.
                sock.settimeout(self.timeout_s)
            self.client.publish(topic, payload, qos=1)
            return True
        except Exception as exc:
            print("mqtt: publish failed:", repr(exc))
            self.close()
            self.failures = 0  # the next publish tries to reconnect right away
            return False
