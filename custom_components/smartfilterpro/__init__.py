from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE

from .const import (
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    # ids
    CONF_USER_ID, CONF_HVAC_ID, CONF_CLIMATE_ENTITY_ID,
    # posting
    CONF_API_BASE, CONF_POST_PATH,
    # tokens
    CONF_ACCESS_TOKEN,
)
from .auth import SfpAuth, is_bubble_soft_401

_LOGGER = logging.getLogger(__name__)

ACTIVE_ACTIONS = {"heating", "cooling", "fan"}
ENTRY_VERSION = 2


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version is None:
        entry.version = 1
    if entry.version == 1:
        data = {**entry.data}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated SmartFilterPro entry from v1 to v2")
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_valid_token(hass: HomeAssistant, entry: ConfigEntry) -> Optional[str]:
    """Centralized check via SfpAuth; returns latest access token."""
    auth = SfpAuth(hass, entry)
    await auth.ensure_valid()
    # fetch most recent token from config entry
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    token = (updated.data if updated else entry.data).get(CONF_ACCESS_TOKEN)
    if token:
        _LOGGER.debug("SFP using access token (len=%s).", len(str(token)))
    else:
        _LOGGER.warning("SFP no access token available; requests will be unauthenticated.")
    return token


def _build_payload(
    state,
    user_id: str,
    hvac_id: str,
    entity_id: str,
    runtime_seconds: Optional[int] = None,
    cycle_start: Optional[str] = None,
    cycle_end: Optional[str] = None,
    connected: bool = False,
    device_name: Optional[str] = None,
) -> dict:
    """Original Bubble payload shape (what your backend expects)."""
    attrs = state.attributes if state else {}
    hvac_action = attrs.get("hvac_action")
    is_active = hvac_action in ACTIVE_ACTIONS
    return {
        "user_id": user_id,
        "hvac_id": hvac_id,
        "ha_entity_id": entity_id,
        "ts": _now_iso(),
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


async def async_setup(hass: HomeAssistant, config: dict):
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up telemetry watcher (if a climate entity was chosen) and load platforms."""
    api_base = (entry.data.get(CONF_API_BASE) or "").rstrip("/")
    post_path = (entry.data.get(CONF_POST_PATH) or "").strip("/")
    user_id = entry.data.get(CONF_USER_ID)
    hvac_id = entry.data.get(CONF_HVAC_ID)
    climate_eid = entry.data.get(CONF_CLIMATE_ENTITY_ID)  # optional

    if not api_base or not post_path or not user_id or not hvac_id:
        _LOGGER.error("SFP missing required config (api_base/post_path/user_id/hvac_id). Telemetry disabled.")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    telemetry_url = f"{api_base}/{post_path}"
    session = async_get_clientsession(hass)

    run_state = {"active_since": None, "last_action": None}

    async def _post(payload: dict) -> None:
        token = await _ensure_valid_token(hass, entry)
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        _LOGGER.debug("SFP POST url=%s headers=%s payload=%s", telemetry_url, list(headers.keys()), payload)
        try:
            async with session.post(telemetry_url, json=payload, headers=headers, timeout=20) as resp:
                txt = await resp.text()

                # Treat true 401s and Bubble soft-401s the same
                if resp.status == 401 or is_bubble_soft_401(txt):
                    _LOGGER.warning(
                        "SFP POST unauthorized (HTTP=%s, soft401=%s). Refreshing and retrying once.",
                        resp.status, is_bubble_soft_401(txt),
                    )
                    await _ensure_valid_token(hass, entry)
                    updated = hass.config_entries.async_get_entry(entry.entry_id)
                    token2 = (updated.data if updated else entry.data).get(CONF_ACCESS_TOKEN)
                    headers2 = dict(headers)
                    if token2:
                        headers2["Authorization"] = f"Bearer {token2}"
                    async with session.post(telemetry_url, json=payload, headers=headers2, timeout=20) as r2:
                        t2 = await r2.text()
                        if r2.status >= 400 or is_bubble_soft_401(t2):
                            _LOGGER.error(
                                "SFP POST retry failed %s -> %s %s | payload=%s",
                                telemetry_url, r2.status, t2[:500], payload
                            )
                        else:
                            _LOGGER.debug("SFP POST retry OK (%s): %s", r2.status, t2[:300])
                    return

                if resp.status >= 400:
                    _LOGGER.error("SFP POST %s -> %s %s | payload=%s", telemetry_url, resp.status, txt[:500], payload)
                else:
                    _LOGGER.debug("SFP POST OK (%s): %s", resp.status, txt[:300])
        except Exception as e:
            _LOGGER.error("SFP POST error: %s", e)

    async def _handle_state(new_state) -> None:
        """Send payload on every climate state change; mark cycle start/stop."""
        hvac_action = (new_state.attributes or {}).get("hvac_action")
        last = run_state["last_action"]
        was_active = last in ACTIVE_ACTIONS
        is_active = hvac_action in ACTIVE_ACTIONS

        payload = None
        now = datetime.now(timezone.utc)

        if not was_active and is_active:
            # cycle start
            run_state["active_since"] = now
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                connected=new_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                device_name=new_state.name,
            )
            _LOGGER.debug("SFP cycle start detected: %s", hvac_action)

        elif was_active and not is_active:
            # cycle end
            start = run_state.get("active_since")
            secs = int((now - start).total_seconds()) if start else 0
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                runtime_seconds=secs,
                cycle_start=start.isoformat() if start else None,
                cycle_end=now.isoformat(),
                connected=new_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                device_name=new_state.name,
            )
            run_state["active_since"] = None
            _LOGGER.debug("SFP cycle end detected; duration=%ss", secs)

        else:
            # steady-state ping
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                connected=new_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                device_name=new_state.name,
            )

        run_state["last_action"] = hvac_action
        if payload:
            await _post(payload)

    @callback
    async def _on_change(event):
        new = event.data.get("new_state")
        if new and (not climate_eid or new.entity_id == climate_eid):
            await _handle_state(new)

    # Only watch telemetry if a climate entity was chosen in the flow
    unsub_telemetry = None
    if climate_eid:
        _LOGGER.debug("SFP telemetry watching %s", climate_eid)
        unsub_telemetry = async_track_state_change_event(hass, [climate_eid], _on_change)

        # Prime an initial send
        st = hass.states.get(climate_eid)
        if st:
            run_state["last_action"] = st.attributes.get("hvac_action")
            if run_state["active_since"] is None and run_state["last_action"] in ACTIVE_ACTIONS:
                run_state["active_since"] = datetime.now(timezone.utc)
            await _handle_state(st)
    else:
        _LOGGER.debug("SFP telemetry disabled (no climate entity chosen)")

    async def _svc_send_now(call):
        if not climate_eid:
            _LOGGER.warning("SFP send_now called but no climate entity configured.")
            return
        s = hass.states.get(climate_eid)
        if s:
            await _post(
                _build_payload(
                    s,
                    user_id=user_id,
                    hvac_id=hvac_id,
                    entity_id=climate_eid,
                    connected=s.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE),
                    device_name=s.name,
                )
            )

    hass.services.async_register(DOMAIN, "send_now", _svc_send_now)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STORAGE_KEY: {"unsub_telemetry": unsub_telemetry}
    }
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data and STORAGE_KEY in data:
        unsub = data[STORAGE_KEY].get("unsub_telemetry")
        if unsub:
            try:
                unsub()
            except Exception:
                pass
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
