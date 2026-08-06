# custom_components/smartfilterpro/__init__.py
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
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

# ---- Outbox (failed-POST retry queue) tuning ----
# Cap on pending entries; oldest is dropped (with a warning) on overflow.
OUTBOX_MAX_ENTRIES = 100
# Backoff schedule between retry attempts, in seconds. Attempts beyond the
# schedule reuse the last step.
OUTBOX_BACKOFF_STEPS = [60, 120, 300, 600, 1800]
# Give up on an entry after this many failed retries (error log, then drop).
OUTBOX_MAX_ATTEMPTS = 10
# How often the drain task wakes up to look for due entries.
OUTBOX_DRAIN_INTERVAL_SECONDS = 60


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
            "last_is_reachable": None,     # bool | None — tracks connectivity transitions
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
                "last_is_reachable": data.get("last_is_reachable"),
            })

        except Exception as e:
            _LOGGER.warning("SFP: Failed to load runtime state: %s", e)

        # Seed fresh/reset counters from epoch milliseconds (Hubitat's proven
        # approach). Core's unique index on (device_id, source_vendor,
        # sequence_number) silently DROPS any event at or below the stored
        # high-water mark for this device — so if this Store file is ever
        # lost/reset, restarting at 1 would make Core discard every event
        # until the counter climbed past the old maximum. An epoch-ms seed
        # always leaps the high-water mark immediately. Existing non-zero
        # counters are left untouched and keep incrementing normally.
        if not self.run_state.get("sequence_number"):
            seed = int(time.time() * 1000)
            self.run_state["sequence_number"] = seed
            _LOGGER.info("SFP: Seeding sequence counter from epoch-ms: %d", seed)
    
    async def save_state(self):
        """Persist current runtime state."""
        try:
            data = {
                "last_action": self.run_state.get("last_action"),
                "is_active": self.run_state.get("is_active", False),
                "last_active_mode": self.run_state.get("last_active_mode"),
                "last_equipment_status": self.run_state.get("last_equipment_status", "Idle"),
                "sequence_number": self.run_state.get("sequence_number", 0),
                "last_is_reachable": self.run_state.get("last_is_reachable"),
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


class Outbox:
    """Store-persisted queue of payloads whose POST to Core failed.

    A failed POST used to be logged and dropped, permanently burning the
    event's already-consumed sequence number — Core's gap detector flagged
    a gap that HA could never answer. Failed payloads now wait here (across
    restarts, via the same HA Store mechanism as RuntimeTracker) and are
    replayed UNCHANGED, so the original sequence_number and dedup keys
    survive. Replay order doesn't matter to Core: its partial unique index
    on (device_id, source_vendor, sequence_number) dedups retries, and its
    sequence tracker advances via GREATEST so out-of-order replays never
    regress the high-water mark.

    Note on gap recovery: Core's ingest response contains NO gap info
    (gap detection runs async after the response is sent), and
    home_assistant is a DIRECT_VENDOR in Core's backfill worker (no bridge
    URL to call back) — detected gaps are terminally marked failed. Gap
    prevention therefore relies entirely on this outbox not dropping
    events.

    Each entry: {"payload": dict, "attempts": int, "next_attempt_at": float
    (epoch seconds)}.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str):
        self.hass = hass
        self._store = Store(hass, 1, f"smartfilterpro_{entry_id}_outbox")
        self.entries: list[dict] = []

    async def load(self):
        """Load pending entries from storage."""
        try:
            data = await self._store.async_load() or {}
            entries = data.get("entries", [])
            if isinstance(entries, list):
                self.entries = [e for e in entries if isinstance(e, dict) and e.get("payload")]
            if self.entries:
                _LOGGER.info("SFP: Outbox loaded %d pending event(s)", len(self.entries))
        except Exception as e:
            _LOGGER.warning("SFP: Failed to load outbox: %s", e)

    async def save(self):
        """Persist pending entries to storage."""
        try:
            await self._store.async_save({"entries": self.entries})
        except Exception as e:
            _LOGGER.warning("SFP: Failed to save outbox: %s", e)

    def add(self, payload: dict):
        """Queue a failed payload for retry (drop-oldest on overflow)."""
        if len(self.entries) >= OUTBOX_MAX_ENTRIES:
            dropped = self.entries.pop(0)
            _LOGGER.warning(
                "SFP: Outbox full (%d entries); dropping oldest pending event "
                "(sequence_number=%s)",
                OUTBOX_MAX_ENTRIES,
                (dropped.get("payload") or {}).get("sequence_number"),
            )
        self.entries.append({
            "payload": payload,
            "attempts": 0,
            "next_attempt_at": time.time() + OUTBOX_BACKOFF_STEPS[0],
        })
        _LOGGER.info(
            "SFP: Queued failed event in outbox (sequence_number=%s, %d pending)",
            payload.get("sequence_number"), len(self.entries),
        )


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


def _discover_humidity_entity_id(hass: HomeAssistant, climate_eid: Optional[str]) -> Optional[str]:
    """Find a humidity sensor entity registered against the same device as the climate entity.

    Many climate integrations (ecobee, Honeywell, Sensibo, etc.) expose the
    thermostat's humidity as a separate `sensor` entity with
    device_class=humidity rather than on the climate entity's attributes.
    Prefer a sensor whose name looks like the "current" / indoor humidity,
    falling back to the first humidity sensor on the device.
    """
    if not climate_eid:
        return None
    try:
        ent_reg = er.async_get(hass)
        ent = ent_reg.async_get(climate_eid)
        if not ent or not ent.device_id:
            return None

        candidates: list[str] = []
        for reg_ent in er.async_entries_for_device(
            ent_reg, ent.device_id, include_disabled_entities=False
        ):
            if reg_ent.domain != "sensor":
                continue
            # Device class can live on the entity registry entry or on the
            # state's attributes; check both.
            dc = (reg_ent.device_class or reg_ent.original_device_class or "").lower()
            if dc != "humidity":
                st = hass.states.get(reg_ent.entity_id)
                st_dc = (st.attributes.get("device_class") if st else "") or ""
                if st_dc.lower() != "humidity":
                    continue
            candidates.append(reg_ent.entity_id)

        if not candidates:
            return None

        # Prefer entities that look like current/indoor humidity over
        # outdoor/forecast sensors.
        def _score(eid: str) -> int:
            low = eid.lower()
            score = 0
            if "outdoor" in low or "outside" in low or "forecast" in low:
                score -= 10
            if "current" in low or "indoor" in low or "inside" in low:
                score += 5
            if low.endswith("_humidity") or low.endswith(".humidity"):
                score += 2
            return score

        candidates.sort(key=_score, reverse=True)
        return candidates[0]
    except Exception as e:
        _LOGGER.debug("SFP humidity sensor discovery failed: %s", e)
        return None


def _read_humidity_from_entity(hass: HomeAssistant, entity_id: Optional[str]) -> Optional[float]:
    """Read current humidity value from a sensor entity; return None if unavailable."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if not st or st.state in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE, "unavailable", "unknown"):
        return None
    try:
        return float(st.state)
    except (TypeError, ValueError):
        return None


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
    humidity_fallback: Optional[float] = None,
) -> dict:
    """
    Payload shape expected by Railway Core (matches Hubitat 8-state format).
    Posts directly to core-ingest-ingest.up.railway.app
    """
    attrs = state.attributes if state else {}
    ts = _now_iso()

    # Get 8-state equipment status
    equipment_status = _classify_8_state(attrs, hvac_mode)
    is_active = equipment_status != "Idle"

    # Map 8-state to boolean flags (matching Hubitat)
    is_cooling = equipment_status in ("Cooling_Fan", "Cooling")
    is_heating = equipment_status in ("Heating_Fan", "Heating", "AuxHeat_Fan", "AuxHeat")
    is_fan_only = equipment_status == "Fan_only"

    # Thermostat mode from HA
    thermostat_mode = hvac_mode

    # Temperature values
    current_temp = attrs.get("current_temperature")
    # Prefer the climate entity's own humidity attribute; many thermostats
    # (ecobee, Honeywell, Sensibo, etc.) don't expose humidity on the climate
    # entity, so fall back to a humidity sensor discovered on the same device.
    humidity = attrs.get("current_humidity")
    if humidity is None:
        humidity = attrs.get("humidity")
    if humidity is None:
        humidity = humidity_fallback
    heat_setpoint = attrs.get("target_temp_low") or attrs.get("temperature")
    cool_setpoint = attrs.get("target_temp_high") or attrs.get("temperature")

    # Determine event type
    if event_type is None:
        if runtime_seconds is not None:
            event_type = "Mode_Change"
        else:
            event_type = "Telemetry_Update"

    return {
        # Device identification
        "device_id": hvac_id,
        "workspace_id": user_id,
        "user_id": user_id,
        "device_name": device_name or entity_id,
        "manufacturer": thermostat_manufacturer or "Home Assistant",
        "model": thermostat_model or "Unknown Model",
        "model_number": entity_id,
        "device_type": "thermostat",
        "source": "home_assistant",
        "source_vendor": "home_assistant",
        "connection_source": "home_assistant",
        "frontend_id": hvac_id,
        "firmware_version": None,
        "serial_number": None,
        "timezone": "UTC",

        # 8-state equipment status fields (matching Hubitat)
        "last_mode": thermostat_mode,
        "thermostat_mode": thermostat_mode,
        "last_is_cooling": is_cooling,
        "last_is_heating": is_heating,
        "last_is_fan_only": is_fan_only,
        "last_equipment_status": equipment_status,
        "is_reachable": bool(is_reachable if is_reachable is not None else connected),

        # Temperature data
        "last_temperature": current_temp,
        "temperature_f": current_temp,
        "humidity": humidity,
        "last_humidity": humidity,
        "last_heat_setpoint": heat_setpoint,
        "heat_setpoint_f": heat_setpoint,
        "last_cool_setpoint": cool_setpoint,
        "cool_setpoint_f": cool_setpoint,

        # Event metadata
        "event_type": event_type,
        "equipment_status": equipment_status,
        "is_active": is_active,
        "runtime_seconds": runtime_seconds,
        "runtime_type": runtime_type,
        "previous_status": previous_status,

        # Timestamps
        "timestamp": ts,
        "recorded_at": ts,
        "observed_at": ts,

        # HA-specific fields (for compatibility)
        "ha_entity_id": entity_id,
        "cycle_start_ts": cycle_start,
        "cycle_end_ts": cycle_end,
        "fan_mode": attrs.get("fan_mode"),

        # Raw attributes for debugging
        "payload_raw": dict(attrs),
    }


async def async_setup(hass: HomeAssistant, config: dict):
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up telemetry watcher (if a climate entity was chosen) and load platforms."""
    api_base = (entry.data.get(CONF_API_BASE) or "").rstrip("/")
    user_id = entry.data.get(CONF_USER_ID)
    hvac_id = entry.data.get(CONF_HVAC_ID)
    climate_eid = entry.data.get(CONF_CLIMATE_ENTITY_ID)  # optional

    if not api_base or not user_id or not hvac_id:
        _LOGGER.error("SFP missing required config (api_base/user_id/hvac_id). Telemetry disabled.")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    # Post telemetry directly to Railway Core (like Hubitat does)
    core_ingest_url = CORE_INGEST_URL
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

    # Discover a humidity sensor on the same device as the climate entity.
    # Many thermostats don't expose humidity on the climate entity itself, so
    # we fall back to a sibling sensor with device_class=humidity.
    humidity_entity_id = _discover_humidity_entity_id(hass, climate_eid)
    if humidity_entity_id:
        _LOGGER.info(
            "SFP: discovered humidity sensor %s for climate %s",
            humidity_entity_id, climate_eid,
        )
    elif climate_eid:
        _LOGGER.debug(
            "SFP: no humidity sensor found on the same device as %s; "
            "will rely on climate entity attributes only", climate_eid,
        )

    # Initialize runtime tracker with persistence
    runtime_tracker = RuntimeTracker(hass, entry.entry_id)
    await runtime_tracker.load_state()

    # Persistent retry queue for failed Core posts (see Outbox docstring:
    # Core cannot ask HA to backfill, so gap-free delivery depends on this).
    outbox = Outbox(hass, entry.entry_id)
    await outbox.load()

    async def _post_to_core(payload: dict, is_retry: bool = False) -> bool:
        """Post telemetry directly to Railway Core using Core JWT token."""
        # Get Core JWT token (refreshes automatically if expired)
        auth = SfpAuth(hass, entry)
        core_token = await auth.ensure_core_token_valid()

        if not core_token:
            _LOGGER.warning("SFP: No valid core_token available; skipping Core post")
            return False

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {core_token}"
        }

        # Core expects array of events (batch endpoint)
        body = [payload] if not isinstance(payload, list) else payload

        if is_retry:
            _LOGGER.info("SFP: RETRY - Attempting Core post with refreshed token...")

        _LOGGER.debug("SFP POST url=%s payload=%s", core_ingest_url, payload)

        try:
            async with session.post(core_ingest_url, json=body, headers=headers, timeout=20) as resp:
                txt = await resp.text()

                if resp.status >= 200 and resp.status < 300:
                    _LOGGER.debug("SFP Core POST OK (%s): %s", resp.status, txt[:300])
                    if is_retry:
                        _LOGGER.info("SFP: RETRY SUCCESSFUL!")
                    return True

                # Handle 401 - refresh Core token and retry once
                if resp.status == 401 and not is_retry:
                    _LOGGER.warning("SFP Core POST 401 — refreshing token and retrying")
                    # Force token refresh by getting a new one
                    new_token = await auth._issue_core_token()
                    if new_token:
                        return await _post_to_core(payload, is_retry=True)
                    else:
                        _LOGGER.error("SFP: Failed to refresh core token")
                        return False

                _LOGGER.error("SFP Core POST %s -> %s %s | payload=%s",
                             core_ingest_url, resp.status, txt[:500], payload)
                return False

        except Exception as e:
            _LOGGER.error("SFP Core POST error: %s", e)
            return False

    async def _post(payload: dict) -> None:
        """Post telemetry to Railway Core; queue in the outbox on failure.

        _post_to_core has already done its own 401-refresh retry by the time
        it returns False, so anything that lands here failed for real. The
        payload's sequence number was consumed before the POST, so dropping
        it would leave a gap Core can never backfill — queue it instead.
        """
        if not await _post_to_core(payload):
            outbox.add(payload)
            await outbox.save()

    async def _drain_outbox(_now=None) -> None:
        """Retry queued payloads whose backoff has elapsed.

        Replays the ORIGINAL payload unchanged — sequence_number and dedup
        keys must survive so Core's unique index dedups and its high-water
        mark advances correctly.
        """
        if not outbox.entries:
            return
        now_ts = time.time()
        due = [e for e in outbox.entries if e.get("next_attempt_at", 0) <= now_ts]
        if not due:
            return
        changed = False
        for item in due:
            payload = item["payload"]
            if await _post_to_core(payload):
                outbox.entries.remove(item)
                changed = True
                _LOGGER.info(
                    "SFP: Outbox replay succeeded (sequence_number=%s, %d remaining)",
                    payload.get("sequence_number"), len(outbox.entries),
                )
                continue
            item["attempts"] = item.get("attempts", 0) + 1
            changed = True
            if item["attempts"] >= OUTBOX_MAX_ATTEMPTS:
                outbox.entries.remove(item)
                _LOGGER.error(
                    "SFP: Dropping outbox event after %d failed attempts "
                    "(sequence_number=%s) — Core will see a permanent gap",
                    item["attempts"], payload.get("sequence_number"),
                )
            else:
                step = OUTBOX_BACKOFF_STEPS[
                    min(item["attempts"], len(OUTBOX_BACKOFF_STEPS) - 1)
                ]
                item["next_attempt_at"] = now_ts + step
                _LOGGER.debug(
                    "SFP: Outbox retry %d failed (sequence_number=%s); next in %ds",
                    item["attempts"], payload.get("sequence_number"), step,
                )
        if changed:
            await outbox.save()

    unsub_outbox = async_track_time_interval(
        hass, _drain_outbox, timedelta(seconds=OUTBOX_DRAIN_INTERVAL_SECONDS)
    )

    async def _handle_state(new_state) -> None:
        """Send payload on every climate state change; mark cycle start/stop."""
        if not _is_climate_available(new_state):
            _LOGGER.debug("SFP: Device unavailable, posting is_reachable=false: %s",
                          new_state.state if new_state else "None")

            # If there was an active cycle running, close it before reporting offline
            was_active = bool(runtime_tracker.run_state.get("is_active"))
            now = datetime.now(timezone.utc)
            previous_status = runtime_tracker.run_state.get("last_equipment_status", "Idle")

            if was_active:
                start = runtime_tracker.run_state.get("active_since")
                secs = _calculate_runtime_seconds(start, now)
                end_payload = _build_payload(
                    new_state,
                    user_id=user_id,
                    hvac_id=hvac_id,
                    entity_id=new_state.entity_id if new_state else climate_eid,
                    hvac_mode=new_state.state if new_state else None,
                    runtime_seconds=secs,
                    cycle_start=start.isoformat() if start else None,
                    cycle_end=now.isoformat(),
                    connected=False,
                    is_reachable=False,
                    device_name=new_state.name if new_state else None,
                    thermostat_manufacturer=device_meta.get("manufacturer"),
                    thermostat_model=device_meta.get("model"),
                    event_type="Mode_Change",
                    previous_status=previous_status,
                    runtime_type="END",
                    humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
                )
                end_payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
                await _post(end_payload)
                runtime_tracker.record_post(previous_status)
                runtime_tracker.run_state["active_since"] = None
                runtime_tracker.run_state["is_active"] = False
                _LOGGER.info("SFP: Closed active cycle (device unreachable); duration=%ss", secs)

            # Post explicit offline/unreachable event so Core knows
            offline_payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id if new_state else climate_eid,
                hvac_mode=new_state.state if new_state else None,
                connected=False,
                is_reachable=False,
                device_name=new_state.name if new_state else None,
                thermostat_manufacturer=device_meta.get("manufacturer"),
                thermostat_model=device_meta.get("model"),
                event_type="CONNECTIVITY_CHANGE",
                previous_status=previous_status,
                humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
            )
            offline_payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
            await _post(offline_payload)
            runtime_tracker.record_post("Idle")
            runtime_tracker.run_state["last_is_reachable"] = False
            await runtime_tracker.save_state()
            return

        # Detect offline → online transition and send CONNECTIVITY_CHANGE
        was_reachable = runtime_tracker.run_state.get("last_is_reachable")
        if was_reachable is False:
            _LOGGER.info("SFP: Device back online, sending CONNECTIVITY_CHANGE (is_reachable=true)")
            online_payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                hvac_mode=new_state.state,
                connected=True,
                is_reachable=True,
                device_name=new_state.name,
                thermostat_manufacturer=device_meta.get("manufacturer"),
                thermostat_model=device_meta.get("model"),
                event_type="CONNECTIVITY_CHANGE",
                previous_status=runtime_tracker.run_state.get("last_equipment_status", "Idle"),
                humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
            )
            online_payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
            await _post(online_payload)
            runtime_tracker.record_post("Idle")

        runtime_tracker.run_state["last_is_reachable"] = True

        attrs = (new_state.attributes or {})
        hvac_mode = new_state.state  # Current thermostat mode (heat/cool/auto/off)
        hvac_action = attrs.get("hvac_action")
        classified_mode = _classify_mode(attrs)  # 'heating' | 'cooling' | 'fanonly' | 'idle'
        is_active = _attrs_is_active(attrs)
        was_active = bool(runtime_tracker.run_state.get("is_active"))

        # Get 8-state equipment status for Core payload
        equipment_status = _classify_8_state(attrs, hvac_mode)
        previous_status = runtime_tracker.run_state.get("last_equipment_status", "Idle")

        _LOGGER.debug(
            "SFP state change: entity=%s, hvac_action=%s, fan_mode=%s, "
            "classified=%s, equipment_status=%s, was_active=%s, is_active=%s",
            new_state.entity_id,
            hvac_action,
            attrs.get("fan_mode"),
            classified_mode,
            equipment_status,
            was_active,
            is_active
        )

        # Maintain last_active_mode so we can report lastMode even while idle
        if classified_mode in ("heating", "cooling", "fanonly"):
            runtime_tracker.run_state["last_active_mode"] = classified_mode

        payload = None
        now = datetime.now(timezone.utc)

        # Pre-resolve humidity fallback once per state change so every payload
        # this handler emits sees the same value.
        humidity_fallback = _read_humidity_from_entity(hass, humidity_entity_id)

        common_kwargs = dict(
            thermostat_manufacturer=device_meta.get("manufacturer"),
            thermostat_model=device_meta.get("model"),
            connected=_is_climate_available(new_state),
            device_name=new_state.name,
            last_mode=runtime_tracker.run_state.get("last_active_mode") if classified_mode == "idle" else classified_mode,
            is_reachable=_is_climate_available(new_state),
            previous_status=previous_status,
            humidity_fallback=humidity_fallback,
        )

        if not was_active and is_active:
            # cycle start
            runtime_tracker.run_state["active_since"] = now
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                hvac_mode=hvac_mode,
                event_type="Mode_Change",
                **common_kwargs,
            )
            _LOGGER.info(
                "SFP cycle start detected: action=%s fan_mode=%s equipment_status=%s",
                hvac_action, attrs.get("fan_mode"), equipment_status
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
                hvac_mode=hvac_mode,
                runtime_seconds=secs,
                cycle_start=start.isoformat() if start else None,
                cycle_end=now.isoformat(),
                last_mode=lm,
                is_reachable=_is_climate_available(new_state),
                thermostat_manufacturer=device_meta.get("manufacturer"),
                thermostat_model=device_meta.get("model"),
                connected=_is_climate_available(new_state),
                device_name=new_state.name,
                event_type="Mode_Change",
                previous_status=previous_status,
                runtime_type="END",
                humidity_fallback=humidity_fallback,
            )
            runtime_tracker.run_state["active_since"] = None
            _LOGGER.info(
                "SFP cycle end detected; duration=%ss (%.1f min) equipment_status=%s previous=%s",
                secs, secs / 60, equipment_status, previous_status
            )

        elif equipment_status != previous_status and is_active:
            # Active-to-active status change (e.g., Cooling → Heating)
            # End the previous segment with runtime, then start a new one
            start = runtime_tracker.run_state.get("active_since")
            secs = _calculate_runtime_seconds(start, now) if start else None

            # Send Mode_Change event to close the previous segment
            end_payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                hvac_mode=hvac_mode,
                runtime_seconds=secs,
                cycle_start=start.isoformat() if start else None,
                cycle_end=now.isoformat(),
                event_type="Mode_Change",
                runtime_type="END",
                previous_status=previous_status,
                **common_kwargs,
            )

            _LOGGER.info(
                "SFP active status change: %s (%ss) → %s",
                previous_status, secs, equipment_status
            )

            end_payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
            await _post(end_payload)
            runtime_tracker.record_post(previous_status)

            # Start a new segment for the new active state
            runtime_tracker.run_state["active_since"] = now

            # Send Telemetry_Update for the new state
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                hvac_mode=hvac_mode,
                event_type="Telemetry_Update",
                **common_kwargs,
            )

        else:
            # steady-state ping (telemetry update) — no status change
            payload = _build_payload(
                new_state,
                user_id=user_id,
                hvac_id=hvac_id,
                entity_id=new_state.entity_id,
                hvac_mode=hvac_mode,
                event_type="Telemetry_Update",
                **common_kwargs,
            )

        # Update last equipment status for next comparison
        runtime_tracker.run_state["last_equipment_status"] = equipment_status

        # Update last seen values
        runtime_tracker.run_state["last_action"] = hvac_action
        runtime_tracker.run_state["is_active"] = is_active

        # Save state after each change
        await runtime_tracker.save_state()

        if payload:
            event_type = payload.get("event_type", "Telemetry_Update")
            # Debounce: skip duplicate Telemetry_Update posts within 3 seconds
            if runtime_tracker.should_skip_duplicate_post(equipment_status, event_type):
                return

            payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
            await _post(payload)
            runtime_tracker.record_post(equipment_status)

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
            equipment_status = _classify_8_state(attrs, st.state)
            if classified_mode in ("heating", "cooling", "fanonly"):
                # Seed last_active_mode if we started mid-cycle
                runtime_tracker.run_state["last_active_mode"] = classified_mode
            runtime_tracker.run_state["last_action"] = attrs.get("hvac_action")
            runtime_tracker.run_state["last_equipment_status"] = equipment_status
            current_active = _attrs_is_active(attrs)
            runtime_tracker.run_state["is_active"] = current_active
            # Seed reachability so the initial _handle_state doesn't
            # fire a spurious CONNECTIVITY_CHANGE on first boot
            if runtime_tracker.run_state.get("last_is_reachable") is None:
                runtime_tracker.run_state["last_is_reachable"] = True

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
            previous_status = runtime_tracker.run_state.get("last_equipment_status", "Idle")
            send_now_payload = _build_payload(
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
                event_type="Telemetry_Update",
                previous_status=previous_status,
                humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
            )
            send_now_payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
            await _post(send_now_payload)

    hass.services.async_register(DOMAIN, "send_now", _svc_send_now)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STORAGE_KEY: {
            "unsub_telemetry": unsub_telemetry,
            "runtime_tracker": runtime_tracker,
            "outbox": outbox,
            "unsub_outbox": unsub_outbox,
        }
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

        unsub_outbox = data[STORAGE_KEY].get("unsub_outbox")
        if unsub_outbox:
            try:
                unsub_outbox()
            except Exception:
                pass

        # Save final state before unloading
        runtime_tracker = data[STORAGE_KEY].get("runtime_tracker")
        if runtime_tracker:
            try:
                await runtime_tracker.save_state()
            except Exception:
                pass

        # Persist any still-pending outbox entries so they survive the unload
        outbox = data[STORAGE_KEY].get("outbox")
        if outbox:
            try:
                await outbox.save()
            except Exception:
                pass
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
