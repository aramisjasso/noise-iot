# Copy this file to config.py and fill in the values for your node.
# NEVER commit config.py: it contains the Wi-Fi password.

WIFI_SSID = ""
WIFI_PASSWORD = ""

# Unique node identifier: lowercase letters, digits and dashes,
# e.g. "tepic-01", "monterrey-01" or "prueba-01" for tests.
NODE_ID = ""

# Where the node is: "tepic" or "monterrey". Goes in the MQTT topic.
SITE = "tepic"

# MQTT broker (laptop server at CICESE, see contracts/mqtt.md).
MQTT_HOST = "158.97.122.248"
MQTT_PORT = 1883

# Sent in session/start; bumped and tagged before the measurement campaign.
FIRMWARE_VERSION = "0.2.0"

# Session length in seconds. 600 for the campaign; use 30 for quick tests.
SESSION_S = 600

# Hardware description, stored with every session.
MIC_MODEL = "KY-037"
GAIN_SETTING = ""  # e.g. "potentiometer at 1/4 turn"; leave "" if unknown

# GPIO of the DHT11 data line (4 on Jesús's prototype).
DHT_PIN = 4

# Hardware watchdog: resets the node if it hangs. Keep True for the campaign;
# set False while working at the REPL, or the board resets every 5 minutes.
WATCHDOG = True
