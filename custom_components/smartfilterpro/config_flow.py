# custom_components/smartfilterpro/config_flow.py
"""Config flow: Sign in with SmartFilterPro, then pick the thermostats.

1. Sign in (Home Assistant's OAuth flow, see oauth.py). If more than one
   environment is registered, Home Assistant first asks which; production
   is listed first.
2. Choose the SmartFilterPro thermostat, when the account has several.
3. Optionally choose the Home Assistant climate entity to forward.

The config entry keeps the shape it always had (access/refresh tokens and
expiry at the top level of entry.data), so auth.py and the rest of the
integration work the same for entries created before this sign-in existed.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Optional

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import selector  # for label/value dropdown

from .auth import normalize_epoch_seconds
from .const import (
    DOMAIN,
    # ids
    CONF_USER_ID, CONF_HVAC_ID, CONF_HVAC_UID, CONF_CLIMATE_ENTITY_ID,
    # endpoints
    CONF_API_BASE, CONF_POST_PATH,
    CONF_RESET_PATH, CONF_STATUS_URL, CONF_REFRESH_PATH,
    # tokens
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT,
    CONF_CORE_TOKEN, CONF_CORE_TOKEN_EXP,
    # environment
    CONF_ENVIRONMENT, CONF_CORE_INGEST_URL, CONF_USE_TEST_ENV,
    ENVIRONMENTS, ENV_PRODUCTION, DEFAULT_ENVIRONMENT,
    # defaults
    DEFAULT_POST_PATH, DEFAULT_RESET_PATH, DEFAULT_STATUS_URL, DEFAULT_REFRESH_PATH,
)
from .oauth import async_register_implementations

_LOGGER = logging.getLogger(__name__)

# Keys hub_token returns (some are aliases older Bubble workflows used)
LOGIN_KEY_MAP = {
    "access_token": ("access_token", "token", "id_token"),
    "refresh_token": ("refresh_token", "rtoken"),
    "user_id": ("user_id", "uid"),
    "hvac_id": ("hvac_id", "primary_hvac_id"),   # may be str or list
    "hvac_ids": ("hvac_ids",),                   # optional list of ids
    "hvac_name": ("hvac_name",),                 # list aligned with hvac_id(s)
}

CHOICE_SKIP = "__SFP_SKIP__"  # sentinel for “skip climate selection”


def _pick(obj: Dict[str, Any], *keys):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _normalize_hvac(val: Any) -> Optional[str]:
    """Ensure HVAC id is a simple string (handles list or stringified list)."""
    if val is None:
        return None
    if isinstance(val, (list, tuple, set)):
        for item in val:
            return str(item)
        return None
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            js = s.replace("'", '"') if ("'" in s and '"' not in s) else s
            arr = json.loads(js)
            if isinstance(arr, Iterable):
                for item in arr:
                    return str(item)
        except Exception:
            s = s.strip("[]").strip().strip("'").strip('"')
            return s or None
    return s or None


def _climate_entity_ids(hass: HomeAssistant) -> list[str]:
    try:
        return sorted(list(hass.states.async_entity_ids("climate")))
    except Exception:
        return []


def thermostats_from(body: Dict[str, Any]) -> tuple[list[str], Dict[str, str]]:
    """Thermostat ids (in order, unique) and their names from a hub_token reply."""
    raw_hvac_id = _pick(body, *LOGIN_KEY_MAP["hvac_id"])
    hvac_ids_alt = _pick(body, *LOGIN_KEY_MAP["hvac_ids"]) or []
    hvac_names = _pick(body, *LOGIN_KEY_MAP["hvac_name"]) or []
    if not isinstance(hvac_names, list):
        hvac_names = [hvac_names]

    ids: list[str] = []
    if isinstance(raw_hvac_id, list):
        ids.extend(str(x) for x in raw_hvac_id if x)
    elif isinstance(raw_hvac_id, (str, int)) and str(raw_hvac_id):
        ids.append(str(raw_hvac_id))
    if isinstance(hvac_ids_alt, list):
        ids.extend(str(x) for x in hvac_ids_alt if x)
    seen: set[str] = set()
    ids = [x for x in ids if not (x in seen or seen.add(x))]

    names: Dict[str, str] = {}
    for idx, _id in enumerate(ids):
        nm = hvac_names[idx] if idx < len(hvac_names) else None
        names[_id] = (str(nm).strip() if isinstance(nm, (str, int)) else None) or _id
    return ids, names


class SmartFilterProConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow for SmartFilterPro."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._login_ctx: Dict[str, Any] = {}
        self._hvac_options: list[dict] = []         # [{"label": "...", "value": "..."}]
        self._hvac_name_by_id: Dict[str, str] = {}  # {"id": "friendly name"}
        self._pending_entry_data: Dict[str, Any] = {}

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    # ------------- Step 1: Sign in with SmartFilterPro -------------
    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        async_register_implementations(self.hass)
        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(self, data: dict) -> FlowResult:
        """hub_token answered with tokens: keep them and move on to the thermostats."""
        body: Dict[str, Any] = data.get("token") or {}
        environment = getattr(self.flow_impl, "environment", DEFAULT_ENVIRONMENT)
        if environment not in ENVIRONMENTS:
            environment = DEFAULT_ENVIRONMENT
        env = ENVIRONMENTS[environment]

        access_token = _pick(body, *LOGIN_KEY_MAP["access_token"])
        refresh_token = _pick(body, *LOGIN_KEY_MAP["refresh_token"])
        user_id = _pick(body, *LOGIN_KEY_MAP["user_id"])
        if not access_token or not user_id:
            # Never log the body: it holds tokens.
            _LOGGER.error("SmartFilterPro sign-in returned no %s", "user id" if access_token else "access token")
            return self.async_abort(reason="oauth_error")

        ids, self._hvac_name_by_id = thermostats_from(body)
        self._hvac_options = [{"label": f"{self._hvac_name_by_id[i]} ({i})", "value": i} for i in ids]

        expires_at = body.get("expires_at")
        self._login_ctx = {
            CONF_ENVIRONMENT: environment,
            CONF_CORE_INGEST_URL: env["core_ingest_url"],
            CONF_API_BASE: env["api_base"].rstrip("/"),
            CONF_POST_PATH: DEFAULT_POST_PATH.strip("/"),
            CONF_RESET_PATH: DEFAULT_RESET_PATH.strip("/"),
            CONF_REFRESH_PATH: DEFAULT_REFRESH_PATH.strip("/"),
            CONF_STATUS_URL: DEFAULT_STATUS_URL.strip("/"),
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_EXPIRES_AT: int(expires_at) if isinstance(expires_at, (int, float)) else None,
            CONF_USER_ID: str(user_id),
            CONF_CORE_TOKEN: body.get("core_token"),
            CONF_CORE_TOKEN_EXP: normalize_epoch_seconds(body.get("core_token_expires_at")),
        }

        if not self._hvac_options:
            _LOGGER.warning("SmartFilterPro: no Home Assistant thermostats on this account")
            return self.async_abort(reason="no_thermostats_found")
        if len(self._hvac_options) == 1:
            return await self._resolve_and_prepare(self._hvac_options[0]["value"])
        return await self.async_step_hvac()

    # ------------- Step 2: Choose SmartFilterPro thermostat -------------
    async def async_step_hvac(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        if user_input is None:
            schema = vol.Schema({
                vol.Required(CONF_HVAC_ID): selector({
                    "select": {
                        "options": self._hvac_options,
                        "mode": "dropdown"
                    }
                })
            })
            return self.async_show_form(step_id="hvac", data_schema=schema, errors={})

        hvac_id = str(user_input[CONF_HVAC_ID])
        return await self._resolve_and_prepare(hvac_id)

    # ------------- Prepare entry; then optional HA climate selection -------------
    async def _resolve_and_prepare(self, hvac_id: Optional[str]) -> FlowResult:
        api_base = self._login_ctx[CONF_API_BASE]
        user_id = self._login_ctx[CONF_USER_ID]
        status_url = f"{api_base}/{self._login_ctx[CONF_STATUS_URL]}"
        bubble_name = self._hvac_name_by_id.get(str(hvac_id)) if hvac_id else None

        # One entry per SmartFilterPro thermostat: adding the same one twice
        # would post its runtime twice.
        await self.async_set_unique_id(f"{user_id}_{hvac_id}")
        self._abort_if_unique_id_configured()

        self._pending_entry_data = {
            CONF_USER_ID: user_id,
            CONF_HVAC_ID: hvac_id,
            CONF_HVAC_UID: hvac_id,
            "hvac_name": bubble_name,                   # <-- keep Bubble-friendly name
            CONF_ENVIRONMENT: self._login_ctx[CONF_ENVIRONMENT],
            CONF_CORE_INGEST_URL: self._login_ctx[CONF_CORE_INGEST_URL],
            CONF_USE_TEST_ENV: self._login_ctx[CONF_ENVIRONMENT] != ENV_PRODUCTION,
            CONF_API_BASE: api_base,
            CONF_POST_PATH: self._login_ctx[CONF_POST_PATH],
            CONF_RESET_PATH: self._login_ctx[CONF_RESET_PATH],
            CONF_STATUS_URL: status_url,
            CONF_REFRESH_PATH: self._login_ctx[CONF_REFRESH_PATH],
            CONF_ACCESS_TOKEN: self._login_ctx.get(CONF_ACCESS_TOKEN),
            CONF_REFRESH_TOKEN: self._login_ctx.get(CONF_REFRESH_TOKEN),
            CONF_EXPIRES_AT: self._login_ctx.get(CONF_EXPIRES_AT),
        }
        if self._login_ctx.get(CONF_CORE_TOKEN):
            self._pending_entry_data[CONF_CORE_TOKEN] = self._login_ctx[CONF_CORE_TOKEN]
            if self._login_ctx.get(CONF_CORE_TOKEN_EXP):
                self._pending_entry_data[CONF_CORE_TOKEN_EXP] = self._login_ctx[CONF_CORE_TOKEN_EXP]
        return await self.async_step_climate()

    # ------------- Step 3: Optional HA climate entity (with Skip) -------------
    async def async_step_climate(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        ha_climates = _climate_entity_ids(self.hass)

        # Always include an explicit Skip option so Submit is enabled
        choices_dict = {CHOICE_SKIP: "Skip (no thermostat)"}
        for eid in ha_climates:
            choices_dict[eid] = eid

        if user_input is None:
            schema = vol.Schema({
                vol.Required(CONF_CLIMATE_ENTITY_ID, default=CHOICE_SKIP): vol.In(choices_dict)
            })
            return self.async_show_form(step_id="climate", data_schema=schema, errors={})

        selection = user_input.get(CONF_CLIMATE_ENTITY_ID)
        data = dict(self._pending_entry_data)

        if selection and selection != CHOICE_SKIP:
            data[CONF_CLIMATE_ENTITY_ID] = selection
            st = self.hass.states.get(selection)
            friendly = st.name if st and getattr(st, "name", None) else selection
            title = f"SmartFilterPro — {friendly}"
        else:
            hv_name = data.get("hvac_name")
            title = f"SmartFilterPro — {hv_name}" if hv_name else "SmartFilterPro"

        return self.async_create_entry(title=title, data=data)
