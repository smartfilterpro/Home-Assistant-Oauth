DOMAIN = "smartfilterpro"

# Some integrations import these; safe to define
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

# Platforms this integration provides
PLATFORMS = ["sensor", "button"]

# ==== Railway Core Ingest URL (same for both Hubitat and HA) ====
CORE_INGEST_URL = "https://core.smartfilterpro.com/ingest/v1/events:batch"

# ==== Config entry keys ====
CONF_USER_ID = "user_id"
CONF_HVAC_ID = "hvac_id"            # selected HVAC id (we also send in body as hvac_uid)
CONF_HVAC_UID = "hvac_uid"          # canonical unique id if/when you have it

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

CONF_API_BASE = "api_base"
CONF_LOGIN_PATH = "login_path"
CONF_POST_PATH = "post_path"
CONF_RESET_PATH = "reset_path"
CONF_STATUS_URL = "status_url"
CONF_REFRESH_PATH = "refresh_path"
CONF_CORE_JWT_PATH = "core_jwt_path"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"      # epoch seconds (UTC)
CONF_CLIMATE_ENTITY_ID = "climate_entity_id"

# Core token storage (for Railway Core authentication)
CONF_CORE_TOKEN = "core_token"
CONF_CORE_TOKEN_EXP = "core_token_exp"  # epoch seconds (UTC)

# ==== Defaults (update base or version when you flip from test→live) ====
DEFAULT_API_BASE = "https://smartfilterpro.com"
DEFAULT_LOGIN_PATH = "/api/1.1/wf/ha_password_login"
DEFAULT_POST_PATH = "/api/1.1/wf/ha_telemetry"
DEFAULT_RESET_PATH = "/api/1.1/wf/ha_reset_filter"
DEFAULT_STATUS_URL = "/api/1.1/wf/ha_therm_status"
DEFAULT_REFRESH_PATH = "/api/1.1/wf/ha_refresh_token"
DEFAULT_CORE_JWT_PATH = "/api/1.1/wf/issue_core_token_ha"

# Refresh 5 minutes before expiry to avoid clock skew
TOKEN_SKEW_SECONDS = 300

# Core token refresh buffer (refresh 60 seconds before expiry)
CORE_TOKEN_SKEW_SECONDS = 60

# Runtime calculation constants
MAX_RUNTIME_SECONDS = 86400  # 24 hours maximum reasonable runtime
RUNTIME_PERSIST_WINDOW = 3600  # 1 hour - restore active cycles within this window after restart
