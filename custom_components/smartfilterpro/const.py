# custom_components/smartfilterpro/const.py
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
CONF_LOGIN_PATH = "login_path"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# Tokens (stored in entry.data after login)
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"  # optional if you later add refresh logic

DEFAULT_API_BASE = "https://smartfilterpro-scaling.bubbleapps.io/version-test/api/1.1/wf/"
DEFAULT_POST_PATH = "ha_telemetry"
DEFAULT_RESOLVER_PATH = "ha_resolve_thermostat_obj"
DEFAULT_RESET_PATH = "ha_reset_filter"
# NEW (Bubble workflow that exchanges email+password -> tokens + user_id)
DEFAULT_LOGIN_PATH = "ha_password_login"

# include button platform
PLATFORMS = ["sensor", "button"]

STORAGE_KEY = "session"
