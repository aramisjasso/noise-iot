"""Temperature and humidity readings from the DHT11.

The sensor is read once every 10 chunks (every 10 seconds) during a
sampling window. If a read fails the checksum it is retried once; if the
retry also fails the reading is reported as None so the batch stays valid.
"""

READ_EVERY_CHUNKS = 10
MAX_RETRIES = 1
DHT_PIN = None  # TODO: set the GPIO used by the DHT11 data line


def init_sensor(pin=DHT_PIN):
    """Create the DHT11 driver.

    Args:
        pin: GPIO number of the DHT11 data line.

    Returns:
        dht.DHT11 instance.
    """
    raise NotImplementedError


def should_read(chunk_index):
    """Tell whether the sensor must be read at this chunk.

    Args:
        chunk_index: chunk number within the window, 0-599.

    Returns:
        True every READ_EVERY_CHUNKS chunks.
    """
    raise NotImplementedError


def read(sensor):
    """Read temperature and relative humidity, retrying once on failure.

    Args:
        sensor: instance returned by init_sensor().

    Returns:
        Tuple (temp_c, rh). Both are None if the read and its retry fail.
    """
    raise NotImplementedError
