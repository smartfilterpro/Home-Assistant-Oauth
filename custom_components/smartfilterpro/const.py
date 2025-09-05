DOMAIN = "smartfilterpro"

# Some integrations import these; safe to define
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

# Platforms this integration provides
PLATFORMS = ["sensor", "button"]

# ==== Config entry keys ====
CONF_USER_ID = "user_id"
CONF_HVAC_ID = "hvac_id"            # selected HVAC id (we also send in body as hvac_uid)
CONF_HVAC_UID = "hvac_uid"          # canonical unique id if/when you have it



CONF_EMAIL = "email"
CONF_PASSWORD = "password"

CONF_API_BASE = "api_base"
CONF_LOGIN_PATH = "login_path"
CONF_POST_PATH = "post_path"
CONF_RESOLVER_PATH = "resolver_path"
CONF_RESET_PATH = "reset_path"
CONF_STATUS_URL = "status_url"
CONF_REFRESH_PATH = "refresh_path"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"      # epoch seconds (UTC)
# add to your existing constants
CONF_CLIMATE_ENTITY_ID = "climate_entity_id"


# ==== Defaults (update base or version when you flip from test→live) ====
DEFAULT_API_BASE = "https://smartfilterpro-scaling.bubbleapps.io"
DEFAULT_LOGIN_PATH = "version-test/api/1.1/wf/ha_password_login"
DEFAULT_POST_PATH = "version-test/api/1.1/wf/ha_telemetry"
DEFAULT_RESOLVER_PATH = "version-test/api/1.1/wf/ha_resolve_thermostat_obj"
DEFAULT_RESET_PATH = "version-test/api/1.1/wf/ha_reset_filter"
DEFAULT_STATUS_URL = "version-test/api/1.1/wf/ha_therm_status"
DEFAULT_REFRESH_PATH = "version-test/api/1.1/wf/ha_refresh_token/initialize"

# Refresh 5 minutes before expiry to avoid clock skew
TOKEN_SKEW_SECONDS = 300
