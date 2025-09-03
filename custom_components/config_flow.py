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
    # IDs / entity
    CONF_USER_ID,
    CONF_HVAC_ID,
    CONF_ENTITY_ID,
    # workflow paths
    CONF_API_BASE,
    CONF_POST_PATH,
    CONF_RESOLVER_PATH,
    CONF_RESET_PATH,
    # resolved data-api URL
    CONF_DATA_OBJ_URL,
    # defaults
    DEFAULT_API_BASE,
    DEFAULT_POST_PATH,
    DEFAULT_RESOLVER_PATH,
    DEFAULT_RESET_PATH,
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
    Two-step flow:
      1) Account & Link: enter USER_ID/HVAC_ID (and optional workflow paths);
         resolve hidden Bubble object id via resolver workflow.
      2) Choose Thermostat: pick HA climate entity to forward to Bubble.
    """
    VERSION = 1
    _step1: dict | None = None  # temp store between steps

    async def async_step_user(self, user_input=None):
        """Step 1: capture IDs and resolve Bubble object ID."""
        errors: dict[str, str] = {}

        # sticky defaults so fields don't clear on error
        defaults = {
            CONF_USER_ID: "",
            CONF_HVAC_ID: "",
            CONF_API_BASE: DEFAULT_API_BASE,
            CONF_POST_PATH: DEFAULT_POST_PATH,
            CONF_RESOLVER_PATH: DEFAULT_RESOLVER_PATH,
            CONF_RESET_PATH: DEFAULT_RESET_PATH,
        }
        if user_input:
            defaults.update(user_input)

        if user_input is not None:
            user_id = (user_input.get(CONF_USER_ID) or "").strip()
            hvac_id = (user_input.get(CONF_HVAC_ID) or "").strip()
            api_base = (user_input.get(CONF_API_BASE) or DEFAULT_API_BASE).rstrip("/")
            post_path = (user_input.get(CONF_POST_PATH) or DEFAULT_POST_PATH).strip("/")
            resolver_path = (user_input.get(CONF_RESOLVER_PATH) or DEFAULT_RESOLVER_PATH).strip("/")
            reset_path = (user_input.get(CONF_RESET_PATH) or DEFAULT_RESET_PATH).strip("/")

            if not user_id:
                errors[CONF_USER_ID] = "required"
            if not hvac_id:
                errors[CONF_HVAC_ID] = "required"

            if not errors:
                url = f"{api_base}/{resolver_path}"
                try:
                    async with aiohttp.ClientSession() as s:
                        r = await s.post(url, json={"user_id": user_id, "hvac_id": hvac_id}, timeout=20)
                        txt = await r.text()

                        if r.status >= 400:
                            _LOGGER.error("Resolver %s -> %s %s", url, r.status, txt[:500])
                            errors["base"] = "cannot_connect"
                        else:
                            # Be lenient about Bubble wrappers: {status, response:{obj_id}}
                            try:
                                data = json.loads(txt)
                            except Exception:
                                _LOGGER.error("Resolver returned non-JSON: %s", txt[:500])
                                errors["base"] = "cannot_connect"
                            else:
                                resp = data.get("response", {}) if isinstance(data, dict) else {}
                                obj_id = (data.get("obj_id") if isinstance(data, dict) else None) or resp.get("obj_id")

                                if not obj_id:
                                    _LOGGER.error("Resolver JSON missing obj_id. Body: %s", txt[:500])
                                    errors["base"] = "not_found"
                                else:
                                    # Build the exact Data API URL your sensors will poll
                                    data_obj_url = (
                                        f"https://smartfilterpro.com/version-test/api/1.1/obj/thermostats/{obj_id}"
                                    )
                                    # Stash everything for step 2
                                    self._step1 = {
                                        CONF_USER_ID: user_id,
                                        CONF_HVAC_ID: hvac_id,
                                        CONF_API_BASE: api_base,
                                        CONF_POST_PATH: post_path,
                                        CONF_RESOLVER_PATH: resolver_path,
                                        CONF_RESET_PATH: reset_path,
                                        CONF_DATA_OBJ_URL: data_obj_url,
                                    }
                                    return await self.async_step_select_entity()

                except Exception as e:
                    _LOGGER.exception("Resolver call failed: %s", e)
                    errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(CONF_USER_ID, default=defaults[CONF_USER_ID]): str,
            vol.Required(CONF_HVAC_ID, default=defaults[CONF_HVAC_ID]): str,
            vol.Optional(CONF_API_BASE, default=defaults[CONF_API_BASE]): str,
            vol.Optional(CONF_POST_PATH, default=defaults[CONF_POST_PATH]): str,
            vol.Optional(CONF_RESOLVER_PATH, default=defaults[CONF_RESOLVER_PATH]): str,
            vol.Optional(CONF_RESET_PATH, default=defaults[CONF_RESET_PATH]): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_select_entity(self, user_input=None):
        """Step 2: choose which climate.* to forward."""
        if not self._step1:
            return await self.async_step_user()

        errors: dict[str, str] = {}
        entities = _climate_entities(self.hass)
        if not entities:
            errors["base"] = "no_climate"

        default_entity = next(iter(entities), "")

        if user_input is not None and not errors:
            entity_id = user_input[CONF_ENTITY_ID]
            data = dict(self._step1)
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
