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
MODEL_E = "mpe"  # Mango Power E battery station
MODEL_S = "lbz_inverter"  # Mango Power S / S Pro microinverter
MODEL_GW = "lbz_gw"  # Gateway Lite (grid meter for the S)
SUPPORTED_MODELS = {MODEL_E: "Mango Power E", MODEL_S: "Mango Power S", MODEL_GW: "Gateway Lite"}

# The Gateway Lite's own realtime data is empty on systems without a battery. Its meter
# readings are reported in the realtime data of the root inverter it is paired with.
METER_KEYS = (
    "meterPhaseAVoltage",
    "meterPhaseACurrent",
    "meterPhaseAActivePower",
    "meterPhaseAReactivePower",
    "meterPhaseAApparentPower",
    "meterPhaseAPowerFactor",
    "meterRssi",
)

# Controls exposed by the app's Mango Power E page (sendDeviceCommand)
MAX_AC_INPUT_OPTIONS = ["10", "15", "30"]  # US; 30 A requires the 30 A cable
BACKUP_SOC_OPTIONS = ["85", "90", "95"]

# Options
CONF_CAPACITY_WH = "capacity_wh"
CONF_EXPORT_PRICE = "export_price"  # per kWh, in the Home Assistant currency
CONF_IMPORT_PRICE = "import_price"
CONF_RAW_FIELDS = "raw_fields"  # expose every Bluetooth protocol field as a sensor
DEFAULT_PRICE = 0.0
# Mango Power E: 3.5 kWh LFP (51.2 V nominal, ~67 Ah). Add expansion batteries in the options.
DEFAULT_CAPACITY_WH = 3500

# Energy integration: ignore gaps longer than this (e.g. HA restart / cloud outage)
MAX_INTEGRATION_GAP_S = 300

# Bluetooth (local) - optional, per Mango Power E unit. The unit pushes realtime data about
# every ~15-18 s over BLE, so there is no faster poll to configure.
BLE_MODE_AUTO = "auto"
BLE_MODE_OFF = "off"
BLE_OPTION_PREFIX = "ble_"          # options key: ble_<serial> -> auto / off / scanner source
BLE_POLL_INTERVAL = 15              # s between keepalive requests (the unit sets the real pace)
BLE_STALE_AFTER = 45                # s after which Bluetooth values fall back to the cloud
CLOUD_STALE_AFTER = 300             # s: cloud keeps serving the last values when a unit is off;
                                    # treat live values as stale if the unit hasn't reported for this long
CLOUD_LIVE_KEYS = ("soc", "pvPower", "gridPower", "gridImportPower", "gridExportPower",
                   "loadPower", "loadDcPower", "batteryPower", "inverterTemp")

# Values available from both sources: key -> Bluetooth decoder key (Bluetooth wins when fresh)
BLE_OVERLAY = {
    "soc": "socUser",
    "pvPower": "pvPower",
    "loadPower": "loadPower",
    "loadDcPower": "dcPower",
}
