DOMAIN = "smartfilterpro"

# Some integrations import these; safe to define
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

# Platforms this integration provides
PLATFORMS = ["sensor", "button"]

# ==== Environments ====
#
# An environment is a PAIR: the Bubble app the integration logs into and the
# Core Ingest service it posts telemetry to. They must match — Bubble mints
# the core_token with that environment's secret, so a token from the test
# Bubble is rejected by production Core. (The old "use test environment"
# toggle only switched Bubble and left Core pointed at production, so test
# telemetry never landed anywhere.)
#
# Adding an environment is one entry here plus a label in translations.
ENV_PRODUCTION = "production"
ENV_DEV = "dev"

ENVIRONMENTS = {
    ENV_PRODUCTION: {
        "api_base": "https://smartfilterpro.com",
        "core_ingest_url": "https://core.smartfilterpro.com/ingest/v1/events:batch",
    },
    ENV_DEV: {
        "api_base": "https://smartfilterpro.com/version-test",
        "core_ingest_url": "https://core-ingest-dev.up.railway.app/ingest/v1/events:batch",
    },
}
DEFAULT_ENVIRONMENT = ENV_PRODUCTION

# Production Core Ingest URL. Kept under its old name for callers that have
# not been given an environment; prefer core_ingest_url_for(...).
CORE_INGEST_URL = ENVIRONMENTS[ENV_PRODUCTION]["core_ingest_url"]


def environment_for_api_base(api_base: str | None) -> str:
    """Map a stored Bubble base URL back to its environment name.

    Config entries created before the environment selector only stored
    api_base. Unknown or empty values fall back to production, which is what
    those entries were posting to anyway.
    """
    base = (api_base or "").rstrip("/")
    for name, env in ENVIRONMENTS.items():
        if env["api_base"].rstrip("/") == base:
            return name
    return DEFAULT_ENVIRONMENT


def core_ingest_url_for(environment: str | None, api_base: str | None = None) -> str:
    """Core Ingest URL for an environment, deriving it from api_base if needed."""
    name = environment if environment in ENVIRONMENTS else environment_for_api_base(api_base)
    return ENVIRONMENTS[name]["core_ingest_url"]

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

# Environment selection. CONF_ENVIRONMENT names an ENVIRONMENTS entry;
# CONF_CORE_INGEST_URL is the resolved Core URL stored on the entry so a
# later change to ENVIRONMENTS never silently re-points an existing install.
# CONF_USE_TEST_ENV is the pre-0.1.0 boolean, still written for parity with
# the Hubitat app and read by nothing else.
CONF_ENVIRONMENT = "environment"
CONF_CORE_INGEST_URL = "core_ingest_url"
CONF_USE_TEST_ENV = "use_test_env"

# ==== API environments (kept for older imports) ====
API_BASE_LIVE = ENVIRONMENTS[ENV_PRODUCTION]["api_base"]
API_BASE_TEST = ENVIRONMENTS[ENV_DEV]["api_base"]

# ==== Defaults ====
DEFAULT_API_BASE = API_BASE_LIVE
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

# Runtime timing (checkpoint interval, unconfirmed limit) lives in runtime.py.
