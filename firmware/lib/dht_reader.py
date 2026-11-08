"""Temperature and humidity readings from the DHT11.

The sensor is read once every 10 chunks (every 10 seconds) during a
sampling window. If a read fails (timeout or checksum) it is retried once,
one chunk later: the DHT11 needs about a second between reads, and waiting
for the next chunk avoids stalling the audio capture. If the retry also
fails the reading is reported with null values so the batch stays valid.
"""

READ_EVERY_CHUNKS = 10
DHT_PIN = 13  # data line of the DHT11, as wired on the first prototype


def init_sensor(pin=DHT_PIN):
    """Create the DHT11 driver.

    Args:
        pin: GPIO number of the DHT11 data line.

    Returns:
        dht.DHT11 instance.
    """
    import dht
    from machine import Pin

    return dht.DHT11(Pin(pin))


def should_read(chunk_index):
    """Tell whether the sensor must be read at this chunk.

    Args:
        chunk_index: chunk number within the window, 0-599.

    Returns:
        True every READ_EVERY_CHUNKS chunks (0, 10, 20, ...).
    """
    return chunk_index % READ_EVERY_CHUNKS == 0


def read(sensor):
    """Read temperature and relative humidity once.

    Args:
        sensor: instance returned by init_sensor().

    Returns:
        Tuple (temp_c, rh) as floats, or None if the read failed.
    """
    try:
        sensor.measure()
        return float(sensor.temperature()), float(sensor.humidity())
    except OSError:
        return None


class ClimateRecorder:
    """Collects the readings of one window, with one retry per failure.

    Call tick(n) after every chunk n; readings holds the entries in the
    format of the "climate" array of the contract.
    """

    def __init__(self, sensor):
        self.sensor = sensor
        self.readings = []
        self._retry_from = None

    def _add(self, offset_s, result):
        temp_c, rh = result if result is not None else (None, None)
        self.readings.append({"offset_s": offset_s, "temp_c": temp_c, "rh": rh})

    def tick(self, chunk_index):
        if should_read(chunk_index):
            result = read(self.sensor)
            if result is None:
                self._retry_from = chunk_index
            else:
                self._add(chunk_index, result)
        elif self._retry_from is not None:
            result = read(self.sensor)
            # The offset is the moment of the reading that counts: the retry
            # if it worked, the scheduled one if both failed.
            self._add(chunk_index if result is not None else self._retry_from, result)
            self._retry_from = None

    def finish(self):
        """Close the window: a retry that never happened counts as failed."""
        if self._retry_from is not None:
            self._add(self._retry_from, None)
            self._retry_from = None
        return self.readings
