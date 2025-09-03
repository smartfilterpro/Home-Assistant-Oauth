from __future__ import annotations

import logging
import aiohttp
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    DOMAIN,
    CONF_API_BASE,
    CONF_POST_PATH,
    CONF_USER_ID,
    CONF_HVAC_ID,
    CONF_ENTITY_ID,
    STORAGE_KEY,
    PLATFORMS,
    # for migration
    DEFAULT_RESET_PATH,
    CONF_RESET_PATH,
)

_LOGGER = logging.getLogger(__name__)

# Treat these hvac_action values as "running"
ACTIVE_ACTIONS = {"heating", "cooling", "fan"}
INACTIVE_ACTIONS = {"idle", "off", None}

# Config entry version
ENTRY_VERSION = 2


async def async_migrate_entry(hass, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the latest version."""
    if entry.version is None:
        entry.version = 1

    if entry.version == 1:
        # Add reset_path to existing entries
        data = {**entry.data}
        data.setdefault(CONF_RESET_PATH, DEFAULT_RESET_PATH)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated SmartFilterPro entry from v1 to v2")
        return True

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Forward thermostat state to Bubble and start platforms."""
    api_base = entry.data[CONF_API_BASE].rstrip("/")
    post_path = entry.data[CONF_POST_PATH].strip("/")
    user_id = entry.data[CONF_USER_ID]
    hvac_id = entry.data[CONF_HVAC_ID]
    entity_id = entry.data[CONF_ENTITY_ID]

    telemetry_url = f"{api_base}/{post_path}"
    session = aiohttp.ClientSession()
    run_state = {"active_since": None, "last_action": None}

    async def _post(url, payload):
        try:
            async with session.post(url, json=payload, timeout=20) as resp:
                txt = await resp.text()
                if resp.status >= 400:
                    _LOGGER.error(
                        "SFP POST %s -> %s %s | payload=%s",
                        url, resp.status, txt[:500], payload
                    )
                else:
                    _LOGGER.debug("SFP POST ok: %s", txt[:200])
        except Exception as e:
            _LOGGER.error("SFP POST failed: %s", e)

    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _build_payload(state, runtime_seconds=None, cycle_start=None, cycle_end=None):
        attrs = state.attributes if state else {}
        hvac_action = attrs.get("hvac_action")
        is_active = hvac_action in ACTIVE_ACTIONS
        return {
            "user_id": user_id,
            "hvac_id": hvac_id,
            "ha_entity_id": entity_id,
            "ts": _now().isoformat(),
            "current_temperature": attrs.get("current_temperature"),
            "target_temperature": attrs.get("temperature"),
            "target_temp_high": attrs.get("target_temp_high"),
            "target_temp_low": attrs.get("target_temp_low"),
            "hvac_mode": attrs.get("hvac_mode"),
            "hvac_status": hvac_action,
            "fan_mode": attrs.get("fan_mode"),
            "isActive": is_active,
            # Always include runtime fields
            "runtime_seconds": runtime_seconds,  # null unless cycle ended
            "cycle_start_ts": cycle_start,
            "cycle_end_ts": cycle_end,
            "raw": attrs,
        }

    async def _handle_state(new_state):
        attrs = new_state.attributes if new_state else {}
        hvac_action = attrs.get("hvac_action")
        last = run_state["last_action"]
        was_active = last in ACTIVE_ACTIONS
        is_active = hvac_action in ACTIVE_ACTIONS

        payload = None
        now = _now()

        # OFF -> ON: start cycle
        if not was_active and is_active:
            run_state["active_since"] = now
            payload = _build_payload(new_state, runtime_seconds=None)

        # ON -> OFF: end cycle and send runtime_seconds
        elif was_active and not is_active:
            start = run_state["active_since"]
            if start:
                secs = int((now - start).total_seconds())
                payload = _build_payload(
                    new_state,
                    runtime_seconds=secs,
                    cycle_start=start.isoformat(),
                    cycle_end=now.isoformat(),
                )
            run_state["active_since"] = None

        # No transition but state changed
        else:
            payload = _build_payload(new_state, runtime_seconds=None)

        run_state["last_action"] = hvac_action
        if payload:
            await _post(telemetry_url, payload)

    @callback
    async def _on_change(event):
        new = event.data.get("new_state")
        if new and new.entity_id == entity_id:
            await _handle_state(new)

    # Subscribe to this entity
    unsub = async_track_state_change_event(hass, [entity_id], _on_change)

    # Seed state + initial snapshot
    st = hass.states.get(entity_id)
    if st:
        run_state["last_action"] = st.attributes.get("hvac_action")
        if run_state["active_since"] is None and run_state["last_action"] in ACTIVE_ACTIONS:
            run_state["active_since"] = _now()
        await _handle_state(st)

    # Manual snapshot service
    async def _svc_send_now(call):
        s = hass.states.get(entity_id)
        if s:
            await _post(telemetry_url, _build_payload(s, runtime_seconds=None))
    hass.services.async_register(DOMAIN, "send_now", _svc_send_now)

    # Load platforms (sensors + button)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STORAGE_KEY: {"session": session, "unsub": unsub, "run_state": run_state}
    }
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data and STORAGE_KEY in data:
        try:
            data[STORAGE_KEY]["unsub"]()
        except Exception:
            pass
        try:
            await data[STORAGE_KEY]["session"].close()
        except Exception:
            pass
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
