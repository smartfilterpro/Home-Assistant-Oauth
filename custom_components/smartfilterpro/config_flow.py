from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    # ids
    CONF_USER_ID, CONF_HVAC_ID, CONF_HVAC_UID, CONF_CLIMATE_ENTITY_ID,
    # creds & endpoints
    CONF_EMAIL, CONF_PASSWORD,
    CONF_API_BASE, CONF_LOGIN_PATH, CONF_POST_PATH, CONF_RESOLVER_PATH,
    CONF_RESET_PATH, CONF_STATUS_URL, CONF_REFRESH_PATH,
    # tokens
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT,
    # defaults
    DEFAULT_API_BASE, DEFAULT_LOGIN_PATH, DEFAULT_POST_PATH, DEFAULT_RESOLVER_PATH,
    DEFAULT_RESET_PATH, DEFAULT_STATUS_URL, DEFAULT_REFRESH_PATH,
)

_LOGGER = logging.getLogger(__name__)

# Accept common alternate key names from Bubble
LOGIN_KEY_MAP = {
    "access_token": ("access_token", "token", "id_token"),
    "refresh_token": ("refresh_token", "rtoken"),
    "expires_at": ("expires_at",),
    "expires_in": ("expires_in",),  # seconds; we’ll convert if present
    "user_id": ("user_id", "uid"),
    "hvac_id": ("hvac_id", "primary_hvac_id"),
    "hvacs": ("hvacs",),            # list of objects
    "hvac_ids": ("hvac_ids",),      # list of strings
}


def _pick(obj: Dict[str, Any], *keys):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _normalize_hvac(val: Any) -> Optional[str]:
    """Ensure HVAC id is a plain string, not a list or a stringified list."""
    if val is None:
        return None
    if isinstance(val, (list, tuple, set)):
        for item in val:
            return str(item)
        return None
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        # looks like "['abc']" or '["abc"]'
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
    """List available climate entities to subscribe for telemetry."""
    try:
        return sorted(list(hass.states.async_entity_ids("climate")))
    except Exception:
        return []


STEP_LOGIN_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Optional(CONF_API_BASE, default=DEFAULT_API_BASE): str,
    vol.Optional(CONF_LOGIN_PATH, default=DEFAULT_LOGIN_PATH): str,
    vol.Optional(CONF_POST_PATH, default=DEFAULT_POST_PATH): str,
    vol.Optional(CONF_RESOLVER_PATH, default=DEFAULT_RESOLVER_PATH): str,
    vol.Optional(CONF_RESET_PATH, default=DEFAULT_RESET_PATH): str,
    vol.Optional(CONF_REFRESH_PATH, default=DEFAULT_REFRESH_PATH): str,
    # Optional: override the exact status URL; otherwise we build it from base+default path
    vol.Optional(CONF_STATUS_URL, default=DEFAULT_STATUS_URL): str,
})

STEP_HVAC_SCHEMA = vol.Schema({
    vol.Required(CONF_HVAC_ID): str,
})

# `climate` step’s schema is built dynamically (to include current climate entities)


class SmartFilterProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for SmartFilterPro."""
    VERSION = 1

    def __init__(self) -> None:
        self._login_ctx: Dict[str, Any] = {}
        self._hvac_choices: Dict[str, str] = {}
        self._pending_entry_data: Dict[str, Any] = {}

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        errors: Dict[str, str] = {}

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

        email = user_input[CONF_EMAIL].strip()
        password = user_input[CONF_PASSWORD]

        api_base = user_input.get(CONF_API_BASE, DEFAULT_API_BASE).rstrip("/")
        login_path = user_input.get(CONF_LOGIN_PATH, DEFAULT_LOGIN_PATH).strip("/")
        post_path = user_input.get(CONF_POST_PATH, DEFAULT_POST_PATH).strip("/")
        resolver_path = user_input.get(CONF_RESOLVER_PATH, DEFAULT_RESOLVER_PATH).strip("/")
        reset_path = user_input.get(CONF_RESET_PATH, DEFAULT_RESET_PATH).strip("/")
        refresh_path = user_input.get(CONF_REFRESH_PATH, DEFAULT_REFRESH_PATH).strip("/")
        override_status_url = user_input.get(CONF_STATUS_URL)

        login_url = f"{api_base}/{login_path}"

        # ---- Login call ----
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(login_url, json={"email": email, "password": password}, timeout=25) as resp:
                    txt = await resp.text()
                    if resp.status >= 400:
                        _LOGGER.error("Login %s -> %s %s", login_url, resp.status, txt[:500])
                        errors["base"] = "cannot_connect"
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
        hvac_id_in    = _pick(body, *LOGIN_KEY_MAP["hvac_id"])
        hvacs         = _pick(body, *LOGIN_KEY_MAP["hvacs"]) or []
        hvac_ids      = _pick(body, *LOGIN_KEY_MAP["hvac_ids"]) or []

        if not access_token or not user_id:
            _LOGGER.error("Login response missing access_token/user_id: %s", body)
            errors["base"] = "unknown"
            return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

        # Convert expires_in → expires_at if needed
        if expires_at is None and isinstance(expires_in, (int, float)):
            import time as _t
            expires_at = int(_t.time()) + int(expires_in)

        # Build HVAC choices, if any
        choices: Dict[str, str] = {}
        if isinstance(hvacs, list):
            for it in hvacs:
                if isinstance(it, dict):
                    _id = it.get("id") or it.get("uid") or it.get("hvac_uid") or it.get("hvac_id")
                    if _id:
                        name = it.get("name") or _id
                        choices[str(_id)] = f"{name} ({_id})"
        if isinstance(hvac_ids, list):
            for _id in hvac_ids:
                if _id and str(_id) not in choices:
                    choices[str(_id)] = str(_id)

        self._login_ctx = {
            CONF_EMAIL: email,
            CONF_API_BASE: api_base,
            CONF_LOGIN_PATH: login_path,
            CONF_POST_PATH: post_path,
            CONF_RESOLVER_PATH: resolver_path,
            CONF_RESET_PATH: reset_path,
            CONF_REFRESH_PATH: refresh_path,
            CONF_STATUS_URL: override_status_url.strip() if override_status_url else None,
            # tokens & ids
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_EXPIRES_AT: expires_at,
            CONF_USER_ID: user_id,
        }

        # If a single HVAC is clear, continue; else ask user
        if hvac_id_in:
            return await self._resolve_and_prepare(_normalize_hvac(hvac_id_in))
        if len(choices) == 1:
            only = next(iter(choices.keys()))
            return await self._resolve_and_prepare(_normalize_hvac(only))

        self._hvac_choices = choices
        if not self._hvac_choices:
            # Server can infer from token; proceed without explicit hvac
            return await self._resolve_and_prepare(None)

        return self.async_show_form(
            step_id="hvac",
            data_schema=vol.Schema({
                vol.Required(CONF_HVAC_ID): vol.In(list(self._hvac_choices.keys()))
            }),
            errors={}
        )

    async def async_step_hvac(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="hvac", data_schema=STEP_HVAC_SCHEMA, errors={})
        return await self._resolve_and_prepare(_normalize_hvac(user_input[CONF_HVAC_ID]))

    async def _resolve_and_prepare(self, hvac_id: Optional[str]) -> FlowResult:
        """Optional resolver call (non-fatal), then store entry data and go to climate selection."""
        api_base = self._login_ctx[CONF_API_BASE]
        resolver_path = self._login_ctx[CONF_RESOLVER_PATH]
        user_id = self._login_ctx[CONF_USER_ID]

        try:
            if hvac_id:
                resolver_url = f"{api_base}/{resolver_path}"
                async with aiohttp.ClientSession() as s:
                    async with s.post(resolver_url, json={"user_id": user_id, "hvac_id": hvac_id}, timeout=20) as r:
                        _ = await r.text()  # ignore payload; optional linkage
        except Exception as e:
            _LOGGER.debug("Resolver skipped/failed (non-fatal): %s", e)

        # status URL (override or default)
        status_url = self._login_ctx.get(CONF_STATUS_URL) or f"{api_base.rstrip('/')}/{DEFAULT_STATUS_URL.strip('/')}"

        self._pending_entry_data = {
            CONF_USER_ID: user_id,
            CONF_HVAC_ID: hvac_id,
            CONF_HVAC_UID: hvac_id,                 # canonical uid we’ll send in bodies
            CONF_API_BASE: api_base,
            CONF_POST_PATH: self._login_ctx[CONF_POST_PATH],
            CONF_RESOLVER_PATH: resolver_path,
            CONF_RESET_PATH: self._login_ctx[CONF_RESET_PATH],
            CONF_STATUS_URL: self._login_ctx[CONF_STATUS_URL],
            CONF_REFRESH_PATH: self._login_ctx[CONF_REFRESH_PATH],
            # tokens
            CONF_ACCESS_TOKEN: self._login_ctx.get(CONF_ACCESS_TOKEN),
            CONF_REFRESH_TOKEN: self._login_ctx.get(CONF_REFRESH_TOKEN),
            CONF_EXPIRES_AT: self._login_ctx.get(CONF_EXPIRES_AT),
        }

        # If there are climate entities, ask the user to choose which to watch.
        return await self.async_step_climate()

    @callback
    async def async_step_climate(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        choices = _climate_entity_ids(self.hass)

        if not choices:
            # No climate entities available; finalize without this field
            title_hvac = (self._pending_entry_data.get(CONF_HVAC_ID) or "default")
            return self.async_create_entry(title=f"SmartFilterPro ({title_hvac})", data=self._pending_entry_data)

        if user_input is None:
            return self.async_show_form(
                step_id="climate",
                data_schema=vol.Schema({vol.Required(CONF_CLIMATE_ENTITY_ID): vol.In(choices)}),
                errors={}
            )

        data = dict(self._pending_entry_data)
        data[CONF_CLIMATE_ENTITY_ID] = user_input[CONF_CLIMATE_ENTITY_ID]
        title_hvac = data.get(CONF_HVAC_ID) or "default"
        return self.async_create_entry(title=f"SmartFilterPro ({title_hvac})", data=data)
