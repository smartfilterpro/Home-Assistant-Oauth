# custom_components/smartfilterpro/__init__.py
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er, device_registry as dr
from homeassistant.helpers.storage import Store

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

# Consider these hvac_action values to be "active"
ACTIVE_ACTIONS = {"heating", "cooling", "fan"}

# Fan modes that indicate air is moving even if hvac_action is "idle"
FAN_ACTIVE_MODES = {"on", "on_high", "circulate"}

ENTRY_VERSION = 2

# Maximum reasonable runtime in seconds (24 hours)
MAX_RUNTIME_SECONDS = 86400


class RuntimeTracker:
    """Handles persistent runtime state tracking."""
    
    def __init__(self, hass: HomeAssistant, entry_id: str):
        self.hass = hass
        self._store = Store(hass, 1, f"smartfilterpro_{entry_id}_runtime")
        self.run_state = {
            "active_since": None,        # datetime | None
            "last_action": None,         # last hvac_action (may be 'idle')
            "is_active": False,          # last computed active boolean
            "last_active_mode": None,    # 'heating' | 'cooling' | 'fanonly' | None
        }
    
    async def load_state(self):
        """Load persisted state, with validation for recent active cycles."""
        try:
            data = await self._store.async_load() or {}
            
            # Restore active_since if it was recent (within last hour to handle restarts)
            if "active_since_iso" in data:
                try:
                    stored_time = datetime.fromisoformat(data["active_since_iso"])
                    time_diff = (datetime.now(timezone.utc) - stored_time).total_seconds()
                    if 0 <= time_diff < 3600:  # Within last hour
                        self.run_state["active_since"] = stored_time
                        _LOGGER.debug("SFP: Restored active cycle from %s (%.1f min ago)", 
                                    stored_time.isoformat(), time_diff / 60)
                    else:
                        _LOGGER.debug("SFP: Ignoring stale active cycle from %s (%.1f hours ago)", 
                                    stored_time.isoformat(), time_diff / 3600)
                except Exception as e:
                    _LOGGER.warning("SFP: Failed to restore active_since: %s", e)
            
            # Restore other state
            self.run_state.update({
                "last_action": data.get("last_action"),
                "is_active": bool(data.get("is_active", False)),
                "last_active_mode": data.get("last_active_mode"),
            })
            
        except Exception as e:
            _LOGGER.warning("SFP: Failed to load runtime state: %s", e)
    
    async def save_state(self):
        """Persist current runtime state."""
        try:
            data = {
                "last_action": self.run_state.get("last_action"),
                "is_active": self.run_state.get("is_active", False),
                "last_active_mode": self.run_state.get("last_active_mode"),
            }
            
            if self.run_state.get("active_since"):
                data["active_since_iso"] = self.run_state["active_since"].isoformat()
            
            await self._store.async_save(data)
        except Exception as e:
            _LOGGER.warning("SFP: Failed to save runtime state: %s", e)


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


def _is_climate_available(state) -> bool:
    """Check if climate entity is properly available."""
    if not state:
        return False
    return state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE, "unavailable", "unknown"}


def _attrs_is_active(attrs: dict) -> bool:
    """
    Determine whether the system should be treated as 'active' (moving air).
    Active if hvac_action is in ACTIVE_ACTIONS OR if hvac_action is idle
    but the fan_mode indicates active circulation.
    """
    if not attrs:
        return False
    
    hvac_action = attrs.get("hvac_action")
    
    # Primary check: explicit active actions
    if hvac_action in ACTIVE_ACTIONS:
        return True
    
    # Secondary check: only for idle state with active fan
    if hvac_action == "idle":
        fan_mode = attrs.get("fan_mode")
        if isinstance(fan_mode, str):
            fm = fan_mode.strip().lower()
            return fm in FAN_ACTIVE_MODES
    
    # All other cases (including None, "off", etc.) are inactive
    return False


def _classify_mode(attrs: dict) -> str:
    """
    Return one of: 'heating', 'cooling', 'fanonly', 'idle'
    """
    if not attrs:
        return "idle"
    
    hvac_action = attrs.get("hvac_action")
    fan_mode = attrs.get("fan_mode")
    
    if hvac_action == "heating":
        return "heating"
    if hvac_action == "cooling":
        return "cooling"
    if hvac_action == "fan":
        return "fanonly"
    
    # If idle but fan is actively circulating, treat as fan-only airflow
    if hvac_action == "idle" and isinstance(fan_mode, str):
        fm = fan_mode.strip().lower()
        if fm in FAN_ACTIVE_MODES:
            return "fanonly"
    
    return "idle"


def _calculate_runtime_seconds(start_time: datetime, end_time: datetime) -> int:
    """Calculate runtime with validation."""
    if not start_time or not end_time:
        return 0
    
    delta_seconds = int((end_time - start_time).total_seconds())
    
    # Validate runtime is reasonable
    if delta_seconds < 0:
        _LOGGER.warning(
            "SFP: Negative runtime calculated: %s seconds (start=%s, end=%s)", 
            delta_seconds, start_time.isoformat(), end_time.isoformat()
        )
        return 0
    
    if delta_seconds > MAX_RUNTIME_SECONDS:
        _LOGGER.warning(
            "SFP: Excessive runtime calculated: %s seconds (%.1f hours) - capping at %s seconds",
            delta_seconds, delta_seconds / 3600, MAX_RUNTIME_SECONDS
        )
        return MAX_RUNTIME_SECONDS
    
    return delta_seconds


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
    *,
    runtime_seconds: Optional[int] = None,
    cycle_start: Optional[str] = None,
    cycle_end: Optional[str] = None,
    connected: bool = False,
    device_name: Optional[str] = None,
    thermostat_manufacturer: Optional[str] = None,
    thermostat_model: Optional[str] = None,
    # NEW: last-mode fields + reachability
    last_mode: Optional[str] = None,
    is_reachable: Optional[bool] = None,
) -> dict:
    """Payload shape expected by your backend (Bubble)."""
    attrs = state.attributes if state else {}
    hvac_action = attrs.get("hvac_action")
    is_active = _attrs_is_active(attrs)

    # Normalize last_mode into the booleans you expect
    lm = (last_mode or "").strip().lower() if last_mode else None
    last_is_heating = lm == "heating"
    last_is_cooling = lm == "cooling"
    last_is_fanonly = lm == "fanonly"

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
        # NEW: pass through thermostat hardware identity from HA's device registry
        "thermostat_manufacturer": thermostat_manufacturer,
        "thermostat_model": thermostat_model,
        # NEW: last-mode & equipment fields
        "lastMode": lm,
        "lastIsHeating": last_is_heating,
        "lastIsCooling": last_is_cooling,
        "lastIsFanOnly": last_is_fanonly,
        "lastEquipmentStatus": lm,   # mirrors lastMode per requirement
        "isReachable": bool(is_reachable if is_reachable is not None else connected),
        # Raw attributes for debugging
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

    # Pull thermostat manufacturer/model from HA's device registry (if we have a climate entity)
    device_meta = {"manufacturer": None, "model": None}
    if climate_eid:
        try:
            ent_reg = er.async_get(hass)
            dev_reg = dr.async_get(hass)
            ent = ent_reg.async_get(climate_eid)
            if ent and ent.device_id:
                dev = dev_reg.async_get(ent.device_id)
                if dev:
                    device_meta["manufacturer"] = dev.manufacturer or None
                    device_meta["model"] = dev.model or None
                    _LOGGER.debug(
                        "SFP device meta for %s -> manufacturer=%s model=%s",
                        climate_eid, device_meta["manufacturer"], device_meta["model"]
                    )
        except Exception as e:
            _LOGGER.debug("SFP device meta lookup failed: %s", e)

    # Initialize runtime tracker with persistence
    runtime_tracker = RuntimeTracker(hass, entry.entry_id)
    await runtime_tracker.load_state()

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
        if not _is_climate_available(new_state):
            _LOGGER.debug("SFP: Skipping unavailable state: %s", new_state.state if new_state else "None")
            return

        attrs = (new_state.attributes or {})
        hvac_action = attrs.get("hvac_action")
        classified_mode = _classify_mode(attrs)  # 'heating' | 'cooling' | 'fanonly' | 'idle'
        is_active = _attrs_is_active(attrs)
        was_active = bool(runtime_tracker.run_state.get("is_active"))

        _LOGGER.debug(
            "SFP state change: entity=%s, hvac_action=%s, fan_mode=%s, "
            "classified=%s, was_active=%s, is_active=%s",
            new_state.entity_id,
            hvac_action,
            attrs.get("fan_mode"),
            classified_mode,
            was_active,
            is_active
        )

        # Maintain last_active_mode so we can report lastMode even while idle
        if classified_mode in ("heating", "cooling", "fanonly"):
            runtime_tracker.run_state["last_active_mode"] = classified_mode

        payload = None
        now = datetime.now(timezone.utc)

        common_kwargs = dict(
            thermostat_manufacturer=device_meta.get("manufacturer"),
            thermostat_model=device_meta.get("model"),
            connected=_is_climate_available(new_state),
            device_name=new_state.name,
            last_mode=runtime_tracker.run_state.get("last_active_mode") if classified_mode == "idle" else classified_mode,
            is_reachable=_is_climate_available(new_state),
        )

        if not was_active and is_active:
            # cycle start
            runtime_tracker.run_state["active_since"] = now
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                **common_kwargs,
            )
            _LOGGER.info(
                "SFP cycle start detected: action=%s fan_mode=%s classified=%s",
                hvac_action, attrs.get("fan_mode"), classified_mode
            )

        elif was_active and not is_active:
            # cycle end
            start = runtime_tracker.run_state.get("active_since")
            secs = _calculate_runtime_seconds(start, now)
            
            # For cycle-end, lastMode should reflect the *last active* mode
            lm = runtime_tracker.run_state.get("last_active_mode") or (
                classified_mode if classified_mode in ("heating", "cooling", "fanonly") else None
            )
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                runtime_seconds=secs,
                cycle_start=start.isoformat() if start else None,
                cycle_end=now.isoformat(),
                last_mode=lm,
                is_reachable=_is_climate_available(new_state),
                thermostat_manufacturer=device_meta.get("manufacturer"),
                thermostat_model=device_meta.get("model"),
                connected=_is_climate_available(new_state),
                device_name=new_state.name,
            )
            runtime_tracker.run_state["active_since"] = None
            _LOGGER.info(
                "SFP cycle end detected; duration=%ss (%.1f min) action=%s fan_mode=%s lastMode=%s",
                secs, secs / 60, hvac_action, attrs.get("fan_mode"), lm
            )

        else:
            # steady-state ping
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                **common_kwargs,
            )

        # Update last seen values
        runtime_tracker.run_state["last_action"] = hvac_action
        runtime_tracker.run_state["is_active"] = is_active

        # Save state after each change
        await runtime_tracker.save_state()

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
        if st and _is_climate_available(st):
            attrs = st.attributes or {}
            classified_mode = _classify_mode(attrs)
            if classified_mode in ("heating", "cooling", "fanonly"):
                # Seed last_active_mode if we started mid-cycle
                runtime_tracker.run_state["last_active_mode"] = classified_mode
            runtime_tracker.run_state["last_action"] = attrs.get("hvac_action")
            current_active = _attrs_is_active(attrs)
            runtime_tracker.run_state["is_active"] = current_active
            
            # If we restored an active_since from storage, don't overwrite it
            if runtime_tracker.run_state["active_since"] is None and current_active:
                # If HA/integration just started mid-cycle, true start is unknown; seed to now.
                runtime_tracker.run_state["active_since"] = datetime.now(timezone.utc)
                _LOGGER.info("SFP: Started mid-cycle, seeding active_since to now")
            
            await runtime_tracker.save_state()
            await _handle_state(st)
    else:
        _LOGGER.debug("SFP telemetry disabled (no climate entity chosen)")

    async def _svc_send_now(call):
        if not climate_eid:
            _LOGGER.warning("SFP send_now called but no climate entity configured.")
            return
        s = hass.states.get(climate_eid)
        if s and _is_climate_available(s):
            attrs = s.attributes or {}
            classified_mode = _classify_mode(attrs)
            lm = (runtime_tracker.run_state.get("last_active_mode")
                  if classified_mode == "idle"
                  else classified_mode if classified_mode in ("heating", "cooling", "fanonly") else None)
            await _post(
                _build_payload(
                    s,
                    user_id=user_id,
                    hvac_id=hvac_id,
                    entity_id=climate_eid,
                    connected=_is_climate_available(s),
                    device_name=s.name,
                    thermostat_manufacturer=device_meta.get("manufacturer"),
                    thermostat_model=device_meta.get("model"),
                    last_mode=lm,
                    is_reachable=_is_climate_available(s),
                )
            )

    hass.services.async_register(DOMAIN, "send_now", _svc_send_now)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STORAGE_KEY: {"unsub_telemetry": unsub_telemetry, "runtime_tracker": runtime_tracker}
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
        
        # Save final state before unloading
        runtime_tracker = data[STORAGE_KEY].get("runtime_tracker")
        if runtime_tracker:
            try:
                await runtime_tracker.save_state()
            except Exception:
                pass
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
