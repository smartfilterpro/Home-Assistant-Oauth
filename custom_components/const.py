DOMAIN = "smartfilterpro"

CONF_USER_ID = "user_id"
CONF_HVAC_ID = "hvac_id"
CONF_ENTITY_ID = "entity_id"

CONF_API_BASE = "api_base"
CONF_POST_PATH = "post_path"
CONF_RESOLVER_PATH = "resolver_path"
CONF_DATA_OBJ_URL = "data_obj_url"

# NEW
CONF_RESET_PATH = "reset_path"

DEFAULT_API_BASE = "https://smartfilterpro.com/version-test/api/1.1/wf"
DEFAULT_POST_PATH = "ha_telemetry"
DEFAULT_RESOLVER_PATH = "ha_resolve_thermostat_obj"
# NEW (your Bubble workflow that resets totals)
DEFAULT_RESET_PATH = "ha_reset_filter"

# include button platform
PLATFORMS = ["sensor", "button"]

STORAGE_KEY = "session"
