# Copy this file to config.py and fill in the values for your node.
# NEVER commit config.py: it contains the Wi-Fi password and the ingest key.

WIFI_SSID = ""
WIFI_PASSWORD = ""

# Unique node identifier, e.g. "tepic-01" or "monterrey-01".
NODE_ID = ""

# HTTPS endpoint of the Supabase Edge Function and its key (see server/README.md).
# INGEST_URL looks like "https://<project-ref>.supabase.co/functions/v1/ingest".
INGEST_URL = ""
INGEST_KEY = ""

# Sent in every batch; bumped and tagged before the measurement campaign.
FIRMWARE_VERSION = "0.1.0"

# Microphone calibration in dB, added to every level. Leave 0.0 until the node
# is compared against a sound level meter (see lib/sampler.py).
MIC_CAL_DB = 0.0

# Hardware watchdog: resets the node if it hangs. Keep True for the campaign;
# set False while working at the REPL, or the board resets every 5 minutes.
WATCHDOG = True
