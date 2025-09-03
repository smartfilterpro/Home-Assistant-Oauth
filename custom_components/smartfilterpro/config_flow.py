from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    # IDs / selection
    CONF_USER_ID, CONF_HVAC_ID, CONF_HVAC_UID,
    # endpoints & creds
    CONF_API_BASE, CONF_LOGIN_PATH, CONF_POST_PATH, CONF_RESOLVER_PATH, CONF_RESET_PATH,
    CONF_STATUS_URL, CONF_REFRESH_PATH,
    CONF_EMAIL, CONF_PASSWORD,
    # tokens
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT,
    # defaults
    DEFAULT_API_BASE, DEFAULT_LOGIN_PATH, DEFAULT_POST_PATH, DEFAULT_RESOLVER_PATH,
    DEFAULT_RESET_PATH, DEFAULT_STATUS_PATH, DEFAULT_REFRESH_PATH,
)

_LOGGER = logging.getLogger(__name__)

STEP_LOGIN_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Optional(CONF_API_BASE, default=DEFAULT_API_BASE): str,
    vol.Optional(CONF_LOGIN_PATH, default=DEFAULT_LOGIN_PATH): str,
    vol.Optional(CONF_POST_PATH, default=DEFAULT_POST_PATH): str,
    vol.Optional(CONF_RESOLVER_PATH, default=DEFAULT_RESOLVER_PATH): str,
    vol.Optional(CONF_RESET_PATH, default=DEFAULT_RESET_PATH): str,
    # Optional overrides (advanced)
    vol.Optional(CONF_STATUS_URL): str,
    vol.Optional(CONF_REFRESH_PATH, default=DEFAULT_REFRESH_PATH): str,
})

STEP_HVAC_SCHEMA = vol.Schema({
    vol.Required(CONF_HVAC_ID): str,
})


class SmartFilterProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._login_ctx: Dict[str, Any] = {}
        self._hvac_choices: Dict[str, str] = {}

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
        access_token  = body.get("access_token")
        refresh_token = body.get("refresh_token")
        expires_at    = body.get("expires_at")
        user_id       = body.get("user_id")

        # HVAC indicators from login (optional)
        hvac_id  = body.get("hvac_id") or body.get("primary_hvac_id")
        hvac_ids = body.get("hvac_ids") if isinstance(body.get("hvac_ids"), list) else []
        hvacs    = body.get("hvacs")    if isinstance(body.get("hvacs"), list)    else []

        if not access_token or not user_id:
            _LOGGER.error("Login response missing access_token/user_id: %s", body)
            errors["base"] = "unknown"
            return self.async_show_form(step_id="user", data_schema=STEP_LOGIN_SCHEMA, errors=errors)

        # Build choices if needed
        choices: Dict[str, str] = {}
        for it in hvacs:
            if isinstance(it, dict):
                _id = it.get("id") or it.get("uid") or it.get("hvac_uid") or it.get("hvac_id")
                if _id:
                    name = it.get("name") or _id
                    choices[str(_id)] = f"{name} ({_id})"
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
            # tokens
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_EXPIRES_AT: expires_at,
            # ids
            CONF_USER_ID: user_id,
        }

        # Short-circuit if we already have exactly one target HVAC
        if hvac_id:
            return await self._resolve_and_finish(str(hvac_id))
        if len(choices) == 1:
            only = next(iter(choices.keys()))
            return await self._resolve_and_finish(only)

        self._hvac_choices = choices
        if not self._hvac_choices:
            # proceed without explicit HVAC (server can infer via token)
            return await self._resolve_and_finish(None)

        return self.async_show_form(step_id="hvac", data_schema=vol.Schema({
            vol.Required(CONF_HVAC_ID): vol.In(list(self._hvac_choices.keys()))
        }), errors={})

    async def async_step_hvac(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="hvac", data_schema=STEP_HVAC_SCHEMA, errors={})
        return await self._resolve_and_finish(user_input[CONF_HVAC_ID])

    async def _resolve_and_finish(self, hvac_id: Optional[str]) -> FlowResult:
        """Optionally call resolver; finalize entry with status_url and tokens."""
        api_base = self._login_ctx[CONF_API_BASE]
        resolver_path = self._login_ctx[CONF_RESOLVER_PATH]
        user_id = self._login_ctx[CONF_USER_ID]

        # Optional: call resolver (non-fatal). We keep it to maintain parity.
        try:
            if hvac_id:
                resolver_url = f"{api_base}/{resolver_path}"
                async with aiohttp.ClientSession() as s:
                    async with s.post(resolver_url, json={"user_id": user_id, "hvac_id": hvac_id}, timeout=20) as r:
                        _ = await r.text()  # ignore payload; server-side can validate/link
        except Exception as e:
            _LOGGER.debug("Resolver call skipped/failed (non-fatal): %s", e)

        # Status endpoint to poll (override or default)
        if self._login_ctx.get(CONF_STATUS_URL):
            status_url = self._login_ctx[CONF_STATUS_URL]
        else:
            status_url = f"{api_base.rstrip('/')}/{DEFAULT_STATUS_PATH.strip('/')}"

        # Build final entry data
        final: Dict[str, Any] = {
            CONF_USER_ID: user_id,
            CONF_HVAC_ID: hvac_id,                 # for reference
            CONF_HVAC_UID: hvac_id,                # send in BODY; not in URL
            CONF_API_BASE: api_base,
            CONF_POST_PATH: self._login_ctx[CONF_POST_PATH],
            CONF_RESOLVER_PATH: resolver_path,
            CONF_RESET_PATH: self._login_ctx[CONF_RESET_PATH],
            CONF_STATUS_URL: status_url,
            # tokens
            CONF_ACCESS_TOKEN: self._login_ctx.get(CONF_ACCESS_TOKEN),
            CONF_REFRESH_TOKEN: self._login_ctx.get(CONF_REFRESH_TOKEN),
            CONF_EXPIRES_AT: self._login_ctx.get(CONF_EXPIRES_AT),
            CONF_REFRESH_PATH: self._login_ctx.get(CONF_REFRESH_PATH),
        }

        title = f"SmartFilterPro ({hvac_id or 'default'})"
        return self.async_create_entry(title=title, data=final)
