# custom_components/smartfilterpro/__init__.py
from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE

from .const import (
    DOMAIN,
    CONF_API_BASE,
    CONF_POST_PATH,
    CONF_USER_ID,
    CONF_HVAC_ID,
    CONF_CLIMATE_ENTITY_ID,   # <-- use this
    STORAGE_KEY,
    PLATFORMS,
    DEFAULT_RESET_PATH,
    CONF_RESET_PATH,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_PATH,
    CONF_STATUS_URL,
    DEFAULT_REFRESH_PATH,
    TOKEN_SKEW_SECONDS,
)


# --- Optional OAuth constants (fallbacks if missing) ---
try:
    from .const import (
        CONF_ACCESS_TOKEN,
        CONF_REFRESH_TOKEN,
        CONF_EXPIRES_AT,
        CONF_REFRESH_PATH,
        DEFAULT_REFRESH_PATH,
        TOKEN_SKEW_SECONDS,
    )
    _OAUTH_CONSTS_OK = True
except Exception:
    # Provide safe defaults if your current const.py doesn't define these yet
    CONF_ACCESS_TOKEN = "access_token"
    CONF_REFRESH_TOKEN = "refresh_token"
    CONF_EXPIRES_AT = "expires_at"
    CONF_REFRESH_PATH = "oauth/refresh"
    DEFAULT_REFRESH_PATH = "oauth/refresh"
    TOKEN_SKEW_SECONDS = 60
    _OAUTH_CONSTS_OK = False

_LOGGER = logging.getLogger(__name__)

ACTIVE_ACTIONS = {"heating", "cooling", "fan"}
ENTRY_VERSION = 2


async def async_migrate_entry(hass, entry: ConfigEntry) -> bool:
    if entry.version is None:
        entry.version = 1

    if entry.version == 1:
        data = {**entry.data}
        data.setdefault(CONF_RESET_PATH, DEFAULT_RESET_PATH)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated SmartFilterPro entry from v1 to v2")
        return True
    return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure_valid_token(hass: HomeAssistant, entry: ConfigEntry) -> Optional[str]:
    """Ensure access token is valid, refresh if expired; log lifecycle."""
    data = entry.data or {}
    exp = int(data.get(CONF_EXPIRES_AT) or 0)
    now = int(time.time())

    if not _OAUTH_CONSTS_OK:
        # Using fallbacks—still attempt refresh if these keys exist in entry.data
        _LOGGER.debug("OAuth consts not found in const.py; using fallback keys.")

    if exp:
        _LOGGER.debug("Token check: now=%s exp=%s (skew=%s)", now, exp, TOKEN_SKEW_SECONDS)

    if exp and now >= (exp - TOKEN_SKEW_SECONDS):
        rt = data.get(CONF_REFRESH_TOKEN)
        if not rt:
            _LOGGER.warning("Token expired, but no refresh_token available")
            return data.get(CONF_ACCESS_TOKEN)

        api_base = (data.get(CONF_API_BASE) or "").rstrip("/")
        refresh_path = (data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
        url = f"{api_base}/{refresh_path}"

        _LOGGER.info("Refreshing access token at %s", url)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"refresh_token": rt}, timeout=20) as resp:
                    txt = await resp.text()
                    if resp.status >= 400:
                        _LOGGER.error("Token refresh failed %s -> %s %s", url, resp.status, txt[:300])
                    else:
                        js = json.loads(txt) if txt else {}
                        body = js.get("response", js) if isinstance(js, dict) else {}
                        at = body.get("access_token")
                        new_rt = body.get("refresh_token", rt)
                        new_exp = body.get("expires_at")
                        if at and new_exp:
                            new_data = dict(data)
                            new_data.update({
                                CONF_ACCESS_TOKEN: at,
                                CONF_REFRESH_TOKEN: new_rt,
                                CONF_EXPIRES_AT: int(new_exp),
                            })
                            hass.config_entries.async_update_entry(entry, data=new_data)
                            _LOGGER.info("Token refresh succeeded, new expiry=%s", new_exp)
        except Exception as e:
            _LOGGER.error("Token refresh error: %s", e)

    # Return current token (possibly refreshed)
    return (hass.config_entries.async_get_entry(entry.entry_id).data or {}).get(CONF_ACCESS_TOKEN)


def _build_payload(state, user_id, hvac_id, entity_id,
                   runtime_seconds=None, cycle_start=None, cycle_end=None,
                   connected=False, device_name=None):
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
        "runtime_seconds": runtime_seconds,   # null unless cycle ended
        "cycle_start_ts": cycle_start,        # ISO string or None
        "cycle_end_ts": cycle_end,            # ISO string or None
        "connected": bool(connected),
        "device_name": device_name,
        "raw": attrs,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    api_base = entry.data[CONF_API_BASE].rstrip("/")
    post_path = entry.data[CONF_POST_PATH].strip("/")
    user_id = entry.data[CONF_USER_ID]
    hvac_id = entry.data[CONF_HVAC_ID]
    entity_id = entry.data[CONF_CLIMATE_ENTITY_ID]


    telemetry_url = f"{api_base}/{post_path}"
    session = aiohttp.ClientSession()
    run_state = {"active_since": None, "last_action": None}

    async def _post(url, payload):
        _LOGGER.debug("SFP POST payload -> %s", payload)
        token = await _ensure_valid_token(hass, entry)
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with session.post(url, json=payload, headers=headers, timeout=20) as resp:
                txt = await resp.text()
                if resp.status >= 400:
                    _LOGGER.error("SFP POST %s -> %s %s | payload=%s", url, resp.status, txt[:500], payload)
                else:
                    _LOGGER.debug("SFP POST ok: %s", txt[:200])
        except Exception as e:
            _LOGGER.error("SFP POST failed: %s", e)

    async def _handle_state(new_state):
        hvac_action = (new_state.attributes or {}).get("hvac_action")
        last = run_state["last_action"]
        was_active = last in ACTIVE_ACTIONS
        is_active = hvac_action in ACTIVE_ACTIONS

        payload = None
        now = _now()

        if not was_active and is_active:
            run_state["active_since"] = now
            payload = _build_payload(
                new_state, user_id, hvac_id, entity_id,
                connected=new_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                device_name=new_state.name
            )

        elif was_active and not is_active:
            start = run_state["active_since"]
            if start:
                secs = int((now - start).total_seconds())
                payload = _build_payload(
                    new_state, user_id, hvac_id, entity_id,
                    runtime_seconds=secs,
                    cycle_start=start.isoformat(),
                    cycle_end=now.isoformat(),
                    connected=new_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                    device_name=new_state.name
                )
            run_state["active_since"] = None

        else:
            payload = _build_payload(
                new_state, user_id, hvac_id, entity_id,
                connected=new_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                device_name=new_state.name
            )

        run_state["last_action"] = hvac_action
        if payload:
            await _post(telemetry_url, payload)

    @callback
    async def _on_change(event):
        new = event.data.get("new_state")
        if new and new.entity_id == entity_id:
            await _handle_state(new)

    unsub = async_track_state_change_event(hass, [entity_id], _on_change)

    st = hass.states.get(entity_id)
    if st:
        run_state["last_action"] = st.attributes.get("hvac_action")
        if run_state["active_since"] is None and run_state["last_action"] in ACTIVE_ACTIONS:
            run_state["active_since"] = _now()
        await _handle_state(st)

    async def _svc_send_now(call):
        s = hass.states.get(entity_id)
        if s:
            await _post(
                telemetry_url,
                _build_payload(
                    s, user_id, hvac_id, entity_id,
                    connected=s.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                    device_name=s.name
                )
            )
    hass.services.async_register(DOMAIN, "send_now", _svc_send_now)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STORAGE_KEY: {"session": session, "unsub": unsub, "run_state": run_state}
    }
    entry.async_on_unload(entry.add_update_listener(_reload))
    if not _OAUTH_CONSTS_OK:
        _LOGGER.info("SmartFilterPro OAuth constants not found in const.py; using fallback keys.")
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
