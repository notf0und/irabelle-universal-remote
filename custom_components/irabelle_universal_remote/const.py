"""Constants for Irabelle Universal Remote."""

DOMAIN = "irabelle_universal_remote"
NAME = "Irabelle Universal Remote"

CONF_INFRARED_ENTITY_ID = "infrared_entity_id"
CONF_INFRARED_RECEIVER_ENTITY_ID = "infrared_receiver_entity_id"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_SENSOR_RESTORE_STATE = "power_sensor_restore_state"
CONF_CARRIER_FREQUENCY = "carrier_frequency"
CONF_TIMING_SCALE = "timing_scale"
CONF_NAME = "name"
CONF_PROFILE_CODE = "profile_code"
CONF_SMARTIR_JSON = "smartir_json_text"
CONF_SMARTIR_JSON_FILE = "smartir_json_file"
CONF_LIBRARY_URL = "library_url"
CONF_LIBRARY_QUERY = "library_query"
CONF_LIBRARY_DEVICE_TYPE = "library_device_type"
CONF_LIBRARY_BRAND = "library_brand"
CONF_LIBRARY_MODEL = "library_model"
CONF_LIBRARY_APPLIANCE = "library_appliance"
CONF_LIBRARY_CHOICE = "library_choice"
CONF_TEST_ACTION = "test_action"
CONF_TEST_MODE = "test_mode"
CONF_TEST_FAN = "test_fan"
CONF_TEST_SWING = "test_swing"

LIBRARY_DEVICE_TYPES = ("any", "climate", "fan", "light", "media_player")
LIBRARY_ANY = "any"
LIBRARY_RESULT_LIMIT = 1000
LIBRARY_INDEX_TTL = 6 * 60 * 60  # seconds before the cached index is re-fetched

# Confirmation loop offered before an entry is created.
TEST_ACTIONS = ("send", "confirm", "next", "save_anyway")
# Preferred command to transmit for each platform, most distinctive first.
TEST_COMMANDS: dict[str, tuple[str, ...]] = {
    "media_player": ("off", "on", "mute"),
    "climate": ("off", "on"),
    "fan": ("off", "on"),
    "light": ("off", "on", "toggle"),
}

DEFAULT_CARRIER_FREQUENCY = 38_000
DEFAULT_TIMING_SCALE = 100
MIN_CARRIER_FREQUENCY = 20_000
MAX_CARRIER_FREQUENCY = 60_000
MIN_TIMING_SCALE = 80
MAX_TIMING_SCALE = 120
