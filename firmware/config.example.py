# Copy this file to config.py and fill in the values for your node.
# NEVER commit config.py: it contains the Wi-Fi password and the ingest key.

WIFI_SSID = ""
WIFI_PASSWORD = ""

# Unique node identifier, e.g. "tepic-01" or "monterrey-01".
NODE_ID = ""

# HTTPS endpoint of the Supabase Edge Function and its key.
INGEST_URL = ""
INGEST_KEY = ""

# Sent in every batch; bumped and tagged before the measurement campaign.
FIRMWARE_VERSION = "0.1.0"
