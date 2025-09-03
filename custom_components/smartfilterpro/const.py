from __future__ import annotations

import json
import logging
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    # stored ids/entity
    CONF_USER_ID,
    CONF_HVAC_ID,
    CONF_ENTITY_ID,
    # workflow paths (internal)
    CONF_API_BASE,
    CONF_POST_PATH,
    CONF_RESOLVER_PATH,
    CONF_RESET_PATH,
    CONF_LOGIN_PATH,
    # login creds + tokens
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_EXPIRES_AT,
    # resolved data-api URL
    CONF_DATA_OBJ_URL,
    # defaults
    DEFAULT_API_BASE,
    DEFAULT_POST_PATH,
    DEFAULT_RESOLVER_PATH,
    DEFAULT_RESET_PATH,
    DEFAULT_LOGIN_PATH,
)

_LOGGER = logging.getLogger(__name__)


def _climate_entities(hass) -> dict[str, str]:
    """Return dict: climate entity_id -> pretty label."""
    out: dict[str, str] = {}
    ent_reg = er.async_get(hass)
    for eid in hass.states.async_entity_ids("climate"):
        st = hass.states.get(eid)
        ent = ent_reg.async_get(eid)
        nice = (ent and ent.original_name) or (st and st.name) or eid
        out[eid] = f"{nice} ({eid})"
    return out


class SmartFilterProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Flow:
      1) Email + Password → login → tokens + user_id + (hvac_id or hvac_ids/hvacs)
         - If a single HVAC is provided, continue automatically.
         - If multiple are provided, show Select HVAC step.
      2) Resolve Bubble object id (server-side) → Entity picker → Create entry.
    """
    VERSION = 4
    _login_ctx: dict | None = None   # after login, before resolver
    _final_ctx: dict | None = None   # after resolver, before entity picker

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        defaults = {
            CONF_EMAIL: "",
            CONF_PASSWORD: "",
        }
        if user_input:
            defaults.update(user_input)

        if user_input is not None:
            email = (user_input.get(CONF_EMAIL) or "").strip()
            password = (user_input.get(CONF_PASSWORD) or "")

            if not email:
                errors[CONF_EMAIL] = "required"
            if not password:
                errors[CONF_PASSWORD] = "required"

            if not errors:
                # Internal (hidden) paths
                api_base = DEFAULT_API_BASE.rstrip("/")
                login_path = DEFAULT_LOGIN_PATH.strip("/")
                resolver_path = DEFAULT_RESOLVER_PATH.strip("/")
                post_path = DEFAULT_POST_PATH.strip("/")
                reset_path = DEFAULT_RESET_PATH.strip("/")

                login_url = f"{api_base}/{login_path}"

                try:
                    async with aiohttp.ClientSession() as s:
                        r = await s.post(login_url, json={"email": email, "password": password}, timeout=20)
                        txt = await r.text()
                        if r.status >= 400:
                            _LOGGER.error("Login %s -> %s %s", login_url, r.status, txt[:500])
                            errors["base"] = "cannot_connect"
                        else:
                            try:
                                data = json.loads(txt)
                            except Exception:
                                _LOGGER.error("Login returned non-JSON: %s", txt[:500])
                                errors["base"] = "cannot_connect"
                            else:
                                body = data.get("response", data)

                                access_token = body.get("access_token")
                                refresh_token = body.get("refresh_token")
                                expires_at = body.get("expires_at")
                                user_id = body.get("user_id")

                                # Accept any shape for HVAC(s)
                                raw_hvac_id = body.get("hvac_id") or body.get("primary_hvac_id")
                                hvac_ids = body.get("hvac_ids") or []
                                hvacs = body.get("hvacs") or []

                                # Normalize if hvac_id came back as a list
                                if isinstance(raw_hvac_id, list):
                                    if len(raw_hvac_id) == 1:
                                        raw_hvac_id = raw_hvac_id[0]
                                    else:
                                        hvac_ids = list(raw_hvac_id)
                                        raw_hvac_id = None

                                hvac_id = str(raw_hvac_id) if raw_hvac_id is not None else None

                                if not access_token or not user_id:
                                    _LOGGER.error("Login missing required fields (need access_token, user_id). Body: %s", txt[:500])
                                    errors["base"] = "not_found"
                                else:
                                    self._login_ctx = {
                                        # fixed paths
                                        CONF_API_BASE: api_base,
                                        CONF_POST_PATH: post_path,
                                        CONF_RESOLVER_PATH: resolver_path,
                                        CONF_RESET_PATH: reset_path,
                                        # tokens
                                        CONF_ACCESS_TOKEN: access_token,
                                        CONF_REFRESH_TOKEN: refresh_token,
                                        CONF_EXPIRES_AT: expires_at,
                                        # ids from login
                                        CONF_USER_ID: user_id,
                                        # HVAC choices
                                        "_hvac_id": hvac_id,
                                        "_hvac_ids": hvac_ids,
                                        "_hvacs": hvacs,
                                    }

                                    if hvac_id:
                                        return await self._resolve_and_go(hvac_id)

                                    options = await self._hvac_options()
                                    if not options:
                                        errors["base"] = "not_found"
                                    else:
                                        return await self.async_step_select_hvac()

                except Exception as e:
                    _LOGGER.exception("Login call failed: %s", e)
                    errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(CONF_EMAIL, default=defaults[CONF_EMAIL]): str,
            vol.Required(CONF_PASSWORD, default=defaults[CONF_PASSWORD]): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _hvac_options(self) -> dict[str, str]:
        """Normalize login response into a menu dict: id -> label."""
        if not self._login_ctx:
            return {}
        hvacs = self._login_ctx.get("_hvacs") or []
        ids = self._login_ctx.get("_hvac_ids") or []
        options: dict[str, str] = {}

        # Prefer rich objects first: {"id":"...", "name":"..."}
        for item in hvacs:
            if isinstance(item, dict):
                _id = item.get("id") or ""
                if not _id:
                    continue
                name = item.get("name") or _id
                options[_id] = f"{name} ({_id})" if name != _id else _id
            else:
                _id = str(item)
                options[_id] = _id

        # Add any plain ids not already present
        for _id in ids:
            _id = str(_id)
            options.setdefault(_id, _id)

        return options

    async def async_step_select_hvac(self, user_input=None):
        """Only shown when multiple HVACs are returned by login."""
        if not self._login_ctx:
            return await self.async_step_user()

        options = await self._hvac_options()
        errors: dict[str, str] = {}

        default = next(iter(options), "")

        if user_input is not None:
            chosen = user_input[CONF_HVAC_ID]
            return await self._resolve_and_go(chosen)

        schema = vol.Schema({
            vol.Required(CONF_HVAC_ID, default=default): vol.In(options) if options else str
        })
        return self.async_show_form(step_id="select_hvac", data_schema=schema, errors=errors)

    async def _resolve_and_go(self, hvac_id: str):
        """Call resolver with (user_id, hvac_id), stash final data, then go to entity picker."""
        api_base = self._login_ctx[CONF_API_BASE]
        resolver_path = self._login_ctx[CONF_RESOLVER_PATH]
        post_path = self._login_ctx[CONF_POST_PATH]
        reset_path = self._login_ctx[CONF_RESET_PATH]
        user_id = self._login_ctx[CONF_USER_ID]

        resolver_url = f"{api_base}/{resolver_path}"
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.post(resolver_url, json={"user_id": user_id, "hvac_id": hvac_id}, timeout=20)
                txt = await r.text()
                if r.status >= 400:
                    _LOGGER.error("Resolver %s -> %s %s", resolver_url, r.status, txt[:500])
                    return self.async_abort(reason="cannot_connect")
                try:
                    data = json.loads(txt)
                except Exception:
                    _LOGGER.error("Resolver returned non-JSON: %s", txt[:500])
                    return self.async_abort(reason="cannot_connect")

                resp = data.get("response", {}) if isinstance(data, dict) else {}
                obj_id = (data.get("obj_id") if isinstance(data, dict) else None) or resp.get("obj_id")
                if not obj_id:
                    _LOGGER.error("Resolver JSON missing obj_id. Body: %s", txt[:500])
                    return self.async_abort(reason="not_found")

                data_obj_url = f"https://smartfilterpro.com/version-test/api/1.1/obj/thermostats/{obj_id}"
        except Exception as e:
            _LOGGER.exception("Resolver call failed: %s", e)
            return self.async_abort(reason="cannot_connect")

        # Stash all final data for the entity picker
        self._final_ctx = {
            CONF_USER_ID: user_id,
            CONF_HVAC_ID: hvac_id,
            CONF_API_BASE: api_base,
            CONF_POST_PATH: post_path,
            CONF_RESOLVER_PATH: resolver_path,
            CONF_RESET_PATH: reset_path,
            CONF_DATA_OBJ_URL: data_obj_url,
            CONF_ACCESS_TOKEN: self._login_ctx.get(CONF_ACCESS_TOKEN),
            CONF_REFRESH_TOKEN: self._login_ctx.get(CONF_REFRESH_TOKEN),
            CONF_EXPIRES_AT: self._login_ctx.get(CONF_EXPIRES_AT),
        }
        return await self.async_step_select_entity()

    async def async_step_select_entity(self, user_input=None):
        """Final step: choose which climate.* to forward."""
        if not self._final_ctx:
            return await self.async_step_user()

        errors: dict[str, str] = {}
        entities = _climate_entities(self.hass)
        if not entities:
            errors["base"] = "no_climate"

        default_entity = next(iter(entities), "")

        if user_input is not None and not errors:
            entity_id = user_input[CONF_ENTITY_ID]
            data = dict(self._final_ctx)
            data[CONF_ENTITY_ID] = entity_id
            return self.async_create_entry(title="SmartFilterPro", data=data)

        schema = vol.Schema({
            vol.Required(CONF_ENTITY_ID, default=default_entity): vol.In(entities) if entities else str
        })
        return self.async_show_form(step_id="select_entity", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SmartFilterProOptionsFlow(config_entry)


class SmartFilterProOptionsFlow(config_entries.OptionsFlow):
    """Allow changing which climate entity is bound to this entry."""
    def __init__(self, entry: config_entries.ConfigEntry):
        self.entry = entry

    async def async_step_init(self, user_input=None):
        entities = _climate_entities(self.hass)
        schema = vol.Schema({
            vol.Required(CONF_ENTITY_ID, default=self.entry.data.get(CONF_ENTITY_ID)):
                vol.In(entities) if entities else str
        })
        if user_input is not None:
            new = dict(self.entry.data)
            new[CONF_ENTITY_ID] = user_input[CONF_ENTITY_ID]
            self.hass.config_entries.async_update_entry(self.entry, data=new)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init", data_schema=schema)
