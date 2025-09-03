DOMAIN = "smartfilterpro"

# Existing / common config keys
CONF_USER_ID = "user_id"
CONF_HVAC_ID = "hvac_id"
CONF_ENTITY_ID = "entity_id"

CONF_API_BASE = "api_base"
CONF_LOGIN_PATH = "login_path"
CONF_POST_PATH = "post_path"
CONF_RESOLVER_PATH = "resolver_path"
CONF_RESET_PATH = "reset_path"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"

# Legacy (kept for back-compat; not required now)
CONF_DATA_OBJ_URL = "data_obj_url"

# NEW for status polling
CONF_STATUS_URL = "status_url"     # workflow we poll (no ids in URL)
CONF_HVAC_UID   = "hvac_uid"       # unique id we send IN BODY (optional)

# Defaults (update base/path if you switch from version-test to live)
DEFAULT_API_BASE       = "https://smartfilterpro-scaling.bubbleapps.io"
DEFAULT_LOGIN_PATH     = "version-test/api/1.1/wf/ha_password_login"
DEFAULT_POST_PATH      = "version-test/api/1.1/wf/ha_telemetry"
DEFAULT_RESOLVER_PATH  = "version-test/api/1.1/wf/ha_resolve_thermostat_obj"
DEFAULT_RESET_PATH     = "version-test/api/1.1/wf/ha_reset_filter"

# NEW: the backend status workflow
DEFAULT_STATUS_PATH    = "version-test/api/1.1/wf/ha_therm_status"