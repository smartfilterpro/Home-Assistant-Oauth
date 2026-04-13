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
    CORE_INGEST_URL,
    # ids
    CONF_USER_ID, CONF_HVAC_ID, CONF_CLIMATE_ENTITY_ID,
    # posting
    CONF_API_BASE, CONF_POST_PATH,
    # tokens
    CONF_ACCESS_TOKEN,
)
from .auth import SfpAuth

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
            "active_since": None,          # datetime | None
            "last_action": None,           # last hvac_action (may be 'idle')
            "is_active": False,            # last computed active boolean
            "last_active_mode": None,      # 'heating' | 'cooling' | 'fanonly' | None
            "last_equipment_status": "Idle",  # 8-state system status
            "last_post_time": None,        # datetime of last post (for debounce)
            "last_post_status": None,      # equipment status of last post
            "sequence_number": 0,          # monotonic event sequence counter
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
                "last_equipment_status": data.get("last_equipment_status", "Idle"),
                "sequence_number": int(data.get("sequence_number", 0)),
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
                "last_equipment_status": self.run_state.get("last_equipment_status", "Idle"),
                "sequence_number": self.run_state.get("sequence_number", 0),
            }

            if self.run_state.get("active_since"):
                data["active_since_iso"] = self.run_state["active_since"].isoformat()

            await self._store.async_save(data)
        except Exception as e:
            _LOGGER.warning("SFP: Failed to save runtime state: %s", e)

    def should_skip_duplicate_post(self, equipment_status: str, event_type: str) -> bool:
        """Check if this post should be skipped as a duplicate (debounce)."""
        # Always allow Mode_Change events (cycle start/end with runtime)
        if event_type == "Mode_Change":
            return False

        now = datetime.now(timezone.utc)
        last_time = self.run_state.get("last_post_time")
        last_status = self.run_state.get("last_post_status")

        # Skip if same status posted within last 3 seconds
        if last_time and last_status == equipment_status:
            elapsed = (now - last_time).total_seconds()
            if elapsed < 3.0:
                _LOGGER.debug(
                    "SFP: Skipping duplicate %s post (same status %s, %.1fs ago)",
                    event_type, equipment_status, elapsed
                )
                return True

        return False

    def record_post(self, equipment_status: str):
        """Record that a post was made (for debounce tracking)."""
        self.run_state["last_post_time"] = datetime.now(timezone.utc)
        self.run_state["last_post_status"] = equipment_status

    def get_and_increment_sequence(self) -> int:
        """Get and increment the event sequence number."""
        seq = self.run_state.get("sequence_number", 0) + 1
        self.run_state["sequence_number"] = seq
        return seq


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


def _classify_8_state(attrs: dict, hvac_mode: str = None) -> str:
    """
    Classify thermostat state using 8-state system matching Hubitat:
    Cooling_Fan, Cooling, Heating_Fan, Heating, AuxHeat_Fan, AuxHeat, Fan_only, Idle

    HA Logic:
      - hvac_action tells us what equipment is running
      - fan_mode tells us if fan is explicitly on
      - preset_mode or hvac_mode may indicate aux/emergency heat
    """
    if not attrs:
        return "Idle"

    hvac_action = (attrs.get("hvac_action") or "idle").lower()
    fan_mode = (attrs.get("fan_mode") or "auto").lower()
    preset_mode = (attrs.get("preset_mode") or "").lower()
    hvac_mode_attr = (attrs.get("hvac_mode") or hvac_mode or "").lower()

    cooling_active = hvac_action == "cooling"
    heating_active = hvac_action == "heating"
    fan_explicitly_on = fan_mode in ("on", "on_high", "circulate")
    fan_only_mode = hvac_action == "fan"

    # Check for auxiliary/emergency heat
    # HA may indicate this in preset_mode or hvac_mode
    is_aux_heat = (
        "emergency" in preset_mode or
        "aux" in preset_mode or
        "emergency" in hvac_mode_attr or
        hvac_mode_attr == "heat_cool" and "aux" in hvac_action
    )

    if is_aux_heat and heating_active and fan_explicitly_on:
        return "AuxHeat_Fan"
    elif is_aux_heat and heating_active:
        return "AuxHeat"
    elif cooling_active and fan_explicitly_on:
        return "Cooling_Fan"
    elif cooling_active:
        return "Cooling"
    elif heating_active and fan_explicitly_on:
        return "Heating_Fan"
    elif heating_active:
        return "Heating"
    elif fan_only_mode or fan_explicitly_on:
        return "Fan_only"
    else:
        return "Idle"


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
    hvac_mode: Optional[str] = None,
    runtime_seconds: Optional[int] = None,
    cycle_start: Optional[str] = None,
    cycle_end: Optional[str] = None,
    connected: bool = False,
    device_name: Optional[str] = None,
    thermostat_manufacturer: Optional[str] = None,
    thermostat_model: Optional[str] = None,
    last_mode: Optional[str] = None,
    is_reachable: Optional[bool] = None,
    event_type: Optional[str] = None,
    previous_status: Optional[str] = None,
    runtime_type: Optional[str] = None,
) -> dict:
    """
    Payload shape expected by Railway Core (matches Hubitat 8-state format).
    Posts directly to core-ingest-ingest.up.railway.app
    """
    attrs = state.attributes if state else {}
    ts = _now_iso()

    # Get 8-state equipment status
    equipment_status = _classify_8_state(attrs, hvac_mode)
    
    # Get base mode classification
    base_mode = _classify_mode(attrs)
    
    # Override last_mode if not explicitly provided
    if last_mode is None:
        last_mode = base_mode
    
    # Override is_reachable if not explicitly provided
    if is_reachable is None:
        is_reachable = _is_climate_available(state)

    # Boolean flags based on mode
    is_heating = base_mode == "heating"
    is_cooling = base_mode == "cooling"
    is_fan_only = base_mode == "fanonly"

    # Telemetry
    temp_f = attrs.get("current_temperature")
    humidity = attrs.get("current_humidity")
    heat_setpoint = attrs.get("temperature") if state.state == "heat" else attrs.get("target_temp_low")
    cool_setpoint = attrs.get("temperature") if state.state == "cool" else attrs.get("target_temp_high")

    # Temperature conversion
    temp_c = None
    if temp_f is not None:
        temp_c = round((temp_f - 32) * 5 / 9, 1)

    payload = {
        "device_id": hvac_id,
        "user_id": user_id,
        "workspace_id": user_id,
        "device_name": device_name or attrs.get("friendly_name", "Home Assistant Thermostat"),
        "manufacturer": thermostat_manufacturer or "Unknown",
        "model": thermostat_model,
        "device_type": "thermostat",
        "source": "home_assistant",
        "source_vendor": "home_assistant",
        "connection_source": "home_assistant",
        "timezone": "UTC",
        
        # State snapshot
        "last_mode": last_mode,
        "last_is_heating": is_heating,
        "last_is_cooling": is_cooling,
        "last_is_fan_only": is_fan_only,
        "last_equipment_status": equipment_status,
        "is_reachable": is_reachable,
        
        # Event metadata
        "event_type": event_type or "Telemetry_Update",
        "equipment_status": equipment_status,
        "previous_status": previous_status or "Unknown",
        
        # Telemetry
        "last_temperature": temp_f,
        "temperature_f": temp_f,
        "temperature_c": temp_c,
        "last_humidity": humidity,
        "humidity": humidity,
        "last_heat_setpoint": heat_setpoint,
        "heat_setpoint_f": heat_setpoint,
        "last_cool_setpoint": cool_setpoint,
        "cool_setpoint_f": cool_setpoint,
        "thermostat_mode": state.state if state else "unknown",
        
        # Timestamps
        "timestamp": ts,
        "recorded_at": ts,
        "observed_at": ts,
    }

    # Add runtime if provided
    if runtime_seconds is not None:
        payload["runtime_seconds"] = runtime_seconds
        
    # Add cycle timing if provided
    if cycle_start:
        payload["cycle_start"] = cycle_start
    if cycle_end:
        payload["cycle_end"] = cycle_end

    return payload


async def _build_sfp_payload(
    hass: HomeAssistant,
    user_id: str,
    hvac_id: str,
    climate_entity_id: str,
    event_type: str,
    equipment_status: str,
    previous_status: str,
    runtime_seconds: Optional[float],
    sequence_number: int,
) -> dict:
    """
    Build standardized Core Ingest payload.
    Matches the schema from other bridges (Ecobee, Nest, SmartThings, etc.)
    """
    state = hass.states.get(climate_entity_id)
    if not state:
        _LOGGER.warning("SFP: Climate entity %s not found", climate_entity_id)
        return {}

    attrs = state.attributes
    
    # Device metadata from entity registry
    entity_reg = er.async_get(hass)
    entity_entry = entity_reg.async_get(climate_entity_id)
    device_name = attrs.get("friendly_name", "Home Assistant Thermostat")
    
    manufacturer = "Unknown"
    model = None
    if entity_entry and entity_entry.device_id:
        dev_reg = dr.async_get(hass)
        device_entry = dev_reg.async_get(entity_entry.device_id)
        if device_entry:
            manufacturer = device_entry.manufacturer or "Unknown"
            model = device_entry.model

    # Telemetry
    temp_f = attrs.get("current_temperature")
    humidity = attrs.get("current_humidity")
    heat_setpoint = attrs.get("temperature") if state.state == "heat" else attrs.get("target_temp_low")
    cool_setpoint = attrs.get("temperature") if state.state == "cool" else attrs.get("target_temp_high")
    
    # Mode
    base_mode = _classify_mode(attrs)
    
    # Boolean flags
    is_heating = equipment_status in ("Heating_Fan", "Heating", "AuxHeat_Fan", "AuxHeat")
    is_cooling = equipment_status in ("Cooling_Fan", "Cooling")
    is_fan_only = equipment_status == "Fan_only"
    is_active = equipment_status not in ("Idle", "Off")
    
    # Temperature conversion
    temp_c = None
    if temp_f is not None:
        temp_c = round((temp_f - 32) * 5 / 9, 1)
    
    observed_at = _now_iso()
    
    return {
        "device_id": hvac_id,
        "user_id": user_id,
        "workspace_id": user_id,
        "device_name": device_name,
        "manufacturer": manufacturer,
        "model": model,
        "device_type": "thermostat",
        "source": "home_assistant",
        "source_vendor": "home_assistant",
        "connection_source": "home_assistant",
        "timezone": str(hass.config.time_zone),
        
        # State snapshot
        "last_mode": base_mode,
        "last_is_heating": is_heating,
        "last_is_cooling": is_cooling,
        "last_is_fan_only": is_fan_only,
        "last_equipment_status": equipment_status,
        "is_reachable": _is_climate_available(state),
        
        # Event metadata
        "event_type": event_type,
        "equipment_status": equipment_status,
        "previous_status": previous_status,
        "is_active": is_active,
        "mode": base_mode,
        "runtime_seconds": runtime_seconds,
        "sequence_number": sequence_number,
        
        # Telemetry
        "last_temperature": temp_f,
        "temperature_f": temp_f,
        "temperature_c": temp_c,
        "last_humidity": humidity,
        "humidity": humidity,
        "last_heat_setpoint": heat_setpoint,
        "heat_setpoint_f": heat_setpoint,
        "last_cool_setpoint": cool_setpoint,
        "cool_setpoint_f": cool_setpoint,
        "thermostat_mode": state.state,
        
        # Timestamps
        "timestamp": observed_at,
        "recorded_at": observed_at,
        "observed_at": observed_at,
    }


async def _post_to_core(
    hass: HomeAssistant,
    access_token: str,
    payload: dict,
    label: str = "event"
) -> bool:
    """Post event to Core Ingest API."""
    if not payload:
        _LOGGER.warning("SFP: Empty payload, skipping post")
        return False
    
    url = f"{CORE_INGEST_URL}/ingest/v1/events:batch"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    
    session = async_get_clientsession(hass)
    
    try:
        async with session.post(url, json=[payload], headers=headers, timeout=15) as resp:
            if resp.status == 200:
                _LOGGER.debug("SFP: Posted %s to Core (%s)", label, payload.get("device_id"))
                return True
            else:
                text = await resp.text()
                _LOGGER.error("SFP: Core post failed [%s]: %s", resp.status, text)
                return False
    except Exception as e:
        _LOGGER.error("SFP: Core post exception: %s", e)
        return False


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the SmartFilterPro integration (legacy YAML - not used)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartFilterPro from a config entry."""
    
    hass.data.setdefault(DOMAIN, {})
    
    user_id = entry.data.get(CONF_USER_ID)
    hvac_id = entry.data.get(CONF_HVAC_ID)
    climate_entity_id = entry.data.get(CONF_CLIMATE_ENTITY_ID)
    access_token = entry.data.get(CONF_ACCESS_TOKEN)
    
    if not all([user_id, hvac_id, climate_entity_id, access_token]):
        _LOGGER.error("SFP: Missing required config data")
        return False
    
    _LOGGER.info(
        "SFP: Setting up integration (user=%s, hvac=%s, entity=%s)",
        user_id, hvac_id, climate_entity_id
    )
    
    # Initialize runtime tracker
    tracker = RuntimeTracker(hass, entry.entry_id)
    await tracker.load_state()
    
    # Store in hass.data
    hass.data[DOMAIN][entry.entry_id] = {
        "user_id": user_id,
        "hvac_id": hvac_id,
        "climate_entity_id": climate_entity_id,
        "access_token": access_token,
        "tracker": tracker,
        "auth": SfpAuth(hass, entry),
    }
    
    @callback
    async def handle_state_change(event):
        """Handle climate entity state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        
        if not new_state or not _is_climate_available(new_state):
            _LOGGER.debug("SFP: Climate unavailable, skipping")
            return
        
        new_attrs = new_state.attributes
        old_attrs = old_state.attributes if old_state else {}
        
        new_is_active = _attrs_is_active(new_attrs)
        old_is_active = _attrs_is_active(old_attrs)
        
        new_equipment_status = _classify_8_state(new_attrs)
        old_equipment_status = tracker.run_state.get("last_equipment_status", "Idle")
        
        # Detect mode transitions
        if new_is_active and not old_is_active:
            # Cycle start
            tracker.run_state["active_since"] = datetime.now(timezone.utc)
            tracker.run_state["last_active_mode"] = _classify_mode(new_attrs)
            tracker.run_state["last_equipment_status"] = new_equipment_status
            
            seq = tracker.get_and_increment_sequence()
            payload = await _build_sfp_payload(
                hass, user_id, hvac_id, climate_entity_id,
                event_type="Mode_Change",
                equipment_status=new_equipment_status,
                previous_status=old_equipment_status,
                runtime_seconds=0,
                sequence_number=seq,
            )
            
            if not tracker.should_skip_duplicate_post(new_equipment_status, "Mode_Change"):
                await _post_to_core(hass, access_token, payload, "cycle_start")
                tracker.record_post(new_equipment_status)
            
            await tracker.save_state()
            
        elif old_is_active and not new_is_active:
            # Cycle end
            active_since = tracker.run_state.get("active_since")
            if active_since:
                runtime_sec = (datetime.now(timezone.utc) - active_since).total_seconds()
                runtime_sec = min(runtime_sec, MAX_RUNTIME_SECONDS)
            else:
                runtime_sec = 0
            
            tracker.run_state["active_since"] = None
            tracker.run_state["last_equipment_status"] = new_equipment_status
            
            seq = tracker.get_and_increment_sequence()
            payload = await _build_sfp_payload(
                hass, user_id, hvac_id, climate_entity_id,
                event_type="Mode_Change",
                equipment_status=new_equipment_status,
                previous_status=old_equipment_status,
                runtime_seconds=runtime_sec,
                sequence_number=seq,
            )
            
            if not tracker.should_skip_duplicate_post(new_equipment_status, "Mode_Change"):
                await _post_to_core(hass, access_token, payload, "cycle_end")
                tracker.record_post(new_equipment_status)
            
            await tracker.save_state()
            
        elif new_is_active:
            # Active state telemetry update
            seq = tracker.get_and_increment_sequence()
            payload = await _build_sfp_payload(
                hass, user_id, hvac_id, climate_entity_id,
                event_type="Telemetry_Update",
                equipment_status=new_equipment_status,
                previous_status=old_equipment_status,
                runtime_seconds=None,
                sequence_number=seq,
            )
            
            if not tracker.should_skip_duplicate_post(new_equipment_status, "Telemetry_Update"):
                await _post_to_core(hass, access_token, payload, "telemetry")
                tracker.record_post(new_equipment_status)
    
    # Subscribe to climate entity state changes
    async_track_state_change_event(hass, climate_entity_id, handle_state_change)
    
    # Forward to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    
    return unload_ok
