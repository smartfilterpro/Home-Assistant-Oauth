# custom_components/smartfilterpro/config_flow.py
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import selector  # for label/value dropdown

from .const import (
    DOMAIN,
    # ids
    CONF_USER_ID, CONF_HVAC_ID, CONF_HVAC_UID, CONF_CLIMATE_ENTITY_ID,
    # creds & endpoints
    CONF_EMAIL, CONF_PASSWORD,
    CONF_API_BASE, CONF_LOGIN_PATH, CONF_POST_PATH,
    CONF_RESET_PATH, CONF_STATUS_URL, CONF_REFRESH_PATH,
    # tokens
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT,
    # defaults
    DEFAULT_API_BASE, DEFAULT_LOGIN_PATH, DEFAULT_POST_PATH,
    DEFAULT_RESET_PATH, DEFAULT_STATUS_URL, DEFAULT_REFRESH_PATH,
)

_LOGGER = logging.getLogger(__name__)

# Bubble login keys (some are aliases we accept)
LOGIN_KEY_MAP = {
    "access_token": ("access_token", "token", "id_token"),
    "refresh_token": ("refresh_token", "rtoken"),
    "expires_at": ("expires_at",),
    "expires_in": ("expires_in",),
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


# User-facing login schema (simple - just email/password)
STEP_LOGIN_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
})


class SmartFilterProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for SmartFilterPro."""
    VERSION = 1

    def __init__(self) -> None:
        self._login_ctx: Dict[str, Any] = {}
        self._hvac_options: list[dict] = []         # [{"label": "...", "value": "..."}]
        self._hvac_name_by_id: Dict[str, str] = {}  # {"id": "friendly name"}
        self._pending_entry_data: Dict[str, Any] = {}

    # ------------- Step 1: Login -------------
    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        errors: Dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

        email = user_input[CONF_EMAIL].strip()
        password = user_input[CONF_PASSWORD]

        # Use defaults for all endpoints (not shown in UI)
        api_base = DEFAULT_API_BASE.rstrip("/")
        login_path = DEFAULT_LOGIN_PATH.strip("/")
        post_path = DEFAULT_POST_PATH.strip("/")
        reset_path = DEFAULT_RESET_PATH.strip("/")
        refresh_path = DEFAULT_REFRESH_PATH.strip("/")
        status_url = DEFAULT_STATUS_URL.strip("/")

        login_url = f"{api_base}/{login_path}"

        # Login
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(login_url, json={"email": email, "password": password}, timeout=25) as resp:
                    txt = await resp.text()
                    if resp.status >= 400:
                        _LOGGER.error("Login %s -> %s %s", login_url, resp.status, txt[:500])
                        errors["base"] = "Cannot connect"
                        return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)
                    try:
                        data = json.loads(txt)
                    except Exception:
                        _LOGGER.error("Login non-JSON: %s", txt[:500])
                        errors["base"] = "unknown"
                        return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)
        except Exception as e:
            _LOGGER.exception("Login call failed: %s", e)
            errors["base"] = "cannot_connect"
            return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

        body = data.get("response", data) if isinstance(data, dict) else {}

        access_token  = _pick(body, *LOGIN_KEY_MAP["access_token"])
        refresh_token = _pick(body, *LOGIN_KEY_MAP["refresh_token"])
        expires_at    = _pick(body, *LOGIN_KEY_MAP["expires_at"])
        expires_in    = _pick(body, *LOGIN_KEY_MAP["expires_in"])
        user_id       = _pick(body, *LOGIN_KEY_MAP["user_id"])

        raw_hvac_id   = _pick(body, *LOGIN_KEY_MAP["hvac_id"])  # may be str or list
        hvac_ids_alt  = _pick(body, *LOGIN_KEY_MAP["hvac_ids"]) or []
        hvac_names    = _pick(body, *LOGIN_KEY_MAP["hvac_name"]) or []

        if not access_token or not user_id:
            _LOGGER.error("Login response missing access_token/user_id: %s", body)
            errors["base"] = "unknown"
            return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

        # Convert expires_in to expires_at if needed
        if expires_at is None and isinstance(expires_in, (int, float)):
            import time as _t
            expires_at = int(_t.time()) + int(expires_in)

        # Normalize thermostats
        ids: list[str] = []
        if isinstance(raw_hvac_id, list):
            ids.extend(str(x) for x in raw_hvac_id if x)
        elif isinstance(raw_hvac_id, (str, int)):
            ids.append(str(raw_hvac_id))
        ids.extend(str(x) for x in hvac_ids_alt if x)
        # unique (preserve order)
        seen = set()
        ids = [x for x in ids if not (x in seen or seen.add(x))]

        # Map id -> name from aligned list (if present)
        self._hvac_name_by_id = {}
        for idx, _id in enumerate(ids):
            nm = hvac_names[idx] if idx < len(hvac_names) else None
            name = (str(nm).strip() if isinstance(nm, (str, int)) else None) or str(_id)
            self._hvac_name_by_id[str(_id)] = name

        # Build dropdown options
        self._hvac_options = [{"label": f"{self._hvac_name_by_id[i]} ({i})", "value": i} for i in ids]

        # Stash login context
        self._login_ctx = {
            CONF_EMAIL: email,
            CONF_API_BASE: api_base,
            CONF_LOGIN_PATH: login_path,
            CONF_POST_PATH: post_path,
            CONF_RESET_PATH: reset_path,
            CONF_REFRESH_PATH: refresh_path,
            CONF_STATUS_URL: status_url,
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_EXPIRES_AT: expires_at,
            CONF_USER_ID: user_id,
        }

        # Routing
        if not self._hvac_options:
            _LOGGER.warning("SmartFilterPro: no thermostats found for this account; aborting setup.")
            return self.async_abort(reason="no_thermostats_found")

        if len(self._hvac_options) == 1:
            only = self._hvac_options[0]["value"]
            _LOGGER.debug("SFP: single thermostat -> %s", only)
            return await self._resolve_and_prepare(only)

        return await self.async_step_hvac()

    # ------------- Step 2: Choose Bubble thermostat -------------
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

        self._pending_entry_data = {
            CONF_USER_ID: user_id,
            CONF_HVAC_ID: hvac_id,
            CONF_HVAC_UID: hvac_id,
            "hvac_name": bubble_name,                   # <-- keep Bubble-friendly name
            CONF_API_BASE: api_base,
            CONF_POST_PATH: self._login_ctx[CONF_POST_PATH],
            CONF_RESET_PATH: self._login_ctx[CONF_RESET_PATH],
            CONF_STATUS_URL: status_url,
            CONF_REFRESH_PATH: self._login_ctx[CONF_REFRESH_PATH],
            CONF_ACCESS_TOKEN: self._login_ctx.get(CONF_ACCESS_TOKEN),
            CONF_REFRESH_TOKEN: self._login_ctx.get(CONF_REFRESH_TOKEN),
            CONF_EXPIRES_AT: self._login_ctx.get(CONF_EXPIRES_AT),
        }
        return await self.async_step_climate()

    # ------------- Step 3: Optional HA climate entity (with Skip) -------------
    @callback
    async def async_step_climate(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        try:
            ha_climates = sorted(list(self.hass.states.async_entity_ids("climate")))
        except Exception:
            ha_climates = []

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
