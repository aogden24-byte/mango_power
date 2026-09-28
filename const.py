"""Constants for the Mango Power integration."""
from datetime import timedelta

DOMAIN = "mango_power"

# Auth (Supabase project used by the official app for all production regions)
SUPABASE_URL = "https://bxoujxvtejswxowvjmvn.supabase.co"
SUPABASE_KEY = "sb_publishable_1NDrXG4s1YvslW7npoHTuw_3JvhVFYV"  # public key embedded in the app

# API base per region (from the app's region config)
REGIONS = {
    "US": "https://api.mangopower.com",
    "AU": "https://api.mangopower.com",
    "JP": "https://api.mangopower.com",
    "EU": "https://api-eu.mangopower.com",
    "CN": "https://api.mangopower.com.cn",
}
DEFAULT_REGION = "US"

# Cloudflare in front of the API rejects non-browser user agents (error 1010).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://localhost",
}

CONF_REGION = "region"
CONF_METHOD = "method"
CONF_CODE = "code"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"
CONF_USER_ID = "user_id"

SCAN_INTERVAL = timedelta(seconds=60)

# Models this integration understands (modelData.name from /device/info)
SUPPORTED_MODELS = {"mpe": "Mango Power E"}

# Controls exposed by the app's Mango Power E page (sendDeviceCommand)
MAX_AC_INPUT_OPTIONS = ["10", "15", "30"]  # US; 30 A requires the 30 A cable
BACKUP_SOC_OPTIONS = ["85", "90", "95"]

# Options
CONF_CAPACITY_WH = "capacity_wh"
# Mango Power E: 3.5 kWh LFP (51.2 V nominal, ~67 Ah). Add expansion batteries in the options.
DEFAULT_CAPACITY_WH = 3500

# Energy integration: ignore gaps longer than this (e.g. HA restart / cloud outage)
MAX_INTEGRATION_GAP_S = 300
