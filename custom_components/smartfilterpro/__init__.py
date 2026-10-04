# custom_components/smartfilterpro/__init__.py
from __future__ import annotations

import asyncio
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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er, device_registry as dr
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    core_ingest_url_for,
    # ids
    CONF_USER_ID, CONF_HVAC_ID, CONF_CLIMATE_ENTITY_ID,
    # posting
    CONF_API_BASE, CONF_POST_PATH, CONF_ENVIRONMENT, CONF_CORE_INGEST_URL,
    # tokens
    CONF_ACCESS_TOKEN,
)
from .auth import SfpAuth
from .runtime import (
    CHECKPOINT_INTERVAL,
    attrs_is_active as _attrs_is_active,
    classify_8_state as _classify_8_state,
    classify_mode as _classify_mode,
    clear_session,
    confirm,
    last_confirmed,
    missed_change_times,
    open_session,
    parse_dt,
    piece_event_id,
    session_open,
    take_piece,
    unconfirmed_too_long,
)

_LOGGER = logging.getLogger(__name__)

ENTRY_VERSION = 2

# This integration is configured only through config entries (the UI flow);
# there is nothing to put under `smartfilterpro:` in configuration.yaml.
# hassfest requires this to be stated for integrations that define
# async_setup.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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
            "active_since": None,          # datetime | None — start of the open session
            "reported_until": None,        # datetime | None — runtime posted up to here
            "last_confirmed": None,        # datetime | None — HA last showed it running
            "resumed": False,              # restored after a restart, not confirmed since (not persisted)
            "last_action": None,           # last hvac_action (may be 'idle')
            "is_active": False,            # a session is open (see runtime.session_open)
            "last_active_mode": None,      # 'heating' | 'cooling' | 'fanonly' | None
            "last_equipment_status": "Idle",  # 8-state system status
            "last_post_time": None,        # datetime of last post (for debounce)
            "last_post_status": None,      # equipment status of last post
            "sequence_number": 0,          # monotonic event sequence counter
            "last_is_reachable": None,     # bool | None — tracks connectivity transitions
        }

    async def load_state(self):
        """Load persisted state.

        An open session is restored whatever its age; setup then resumes it
        if HA confirmed it within UNCONFIRMED_LIMIT, or closes it at its last
        confirmation (see _startup in async_setup_entry). The old rule dropped any session
        older than an hour and seeded a new start at "now", losing everything
        before the restart.
        """
        try:
            data = await self._store.async_load() or {}

            active_since = parse_dt(data.get("active_since_iso"))
            self.run_state.update({
                "active_since": active_since,
                # State saved before checkpoints existed has reported nothing,
                # and its only confirmation on record is the session start.
                "reported_until": parse_dt(data.get("reported_until_iso")) or active_since,
                "last_confirmed": parse_dt(data.get("last_confirmed_iso")) or active_since,
                "last_action": data.get("last_action"),
                "is_active": bool(data.get("is_active", False)),
                "last_active_mode": data.get("last_active_mode"),
                "last_equipment_status": data.get("last_equipment_status", "Idle"),
                "sequence_number": int(data.get("sequence_number", 0)),
                "last_is_reachable": data.get("last_is_reachable"),
            })
            if self.run_state["is_active"] and active_since is None:
                # is_active without a start time cannot be accounted for (it
                # used to END with runtime 0, or runtime None on a status
                # change). Treat it as closed; the current state starts anew.
                _LOGGER.warning("SFP: Stored session had no start time; discarding it")
                clear_session(self.run_state)
            if not self.run_state["is_active"]:
                clear_session(self.run_state)

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

            for key in ("active_since", "reported_until", "last_confirmed"):
                if self.run_state.get(key):
                    data[f"{key}_iso"] = self.run_state[key].isoformat()

            await self._store.async_save(data)
        except Exception as e:
            _LOGGER.warning("SFP: Failed to save runtime state: %s", e)

    def raise_sequence_floor(self, floor: int) -> None:
        """Never hand out a sequence number at or below `floor`.

        Events are written to the outbox before the runtime state is saved,
        so after a crash between the two the outbox can hold a number the
        restored counter has not reached; reusing it would make Core drop
        whichever copy arrives second.
        """
        if floor and floor > int(self.run_state.get("sequence_number") or 0):
            self.run_state["sequence_number"] = int(floor)

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
    """Store-persisted log of payloads not yet acknowledged by Core.

    Every event is written here (with its sequence number) BEFORE it is
    posted, and removed once Core acknowledges it — the outbound-log rule
    of @smartfilterpro/bridge-core, ported. If HA dies mid-POST the event is
    still here after the restart and is replayed. A failed POST used to be
    logged and dropped, permanently burning the event's already-consumed
    sequence number — Core's gap detector flagged a gap that HA could never
    answer. Pending payloads wait here (across restarts, via the same HA
    Store mechanism as RuntimeTracker) and are replayed UNCHANGED, so the
    original sequence_number and dedup keys survive. Replay order doesn't
    matter to Core: its partial unique index on (device_id, source_vendor,
    sequence_number) dedups retries, and its
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

    def add(self, payload: dict) -> dict:
        """Log a payload before its POST (drop-oldest on overflow).

        The first retry is OUTBOX_BACKOFF_STEPS[0] away, longer than a POST
        can take, so the drain only picks it up if this attempt failed or
        HA died during it.
        """
        if len(self.entries) >= OUTBOX_MAX_ENTRIES:
            # Drop the oldest event that carries no runtime if there is one:
            # a lost telemetry snapshot is replaced by the next, a lost
            # runtime piece (checkpoint or END) is runtime Core never sees.
            idx = next(
                (i for i, e in enumerate(self.entries)
                 if not ((e.get("payload") or {}).get("runtime_seconds") or 0) > 0),
                0,
            )
            dropped = self.entries.pop(idx)
            _LOGGER.warning(
                "SFP: Outbox full (%d entries); dropping pending event "
                "(sequence_number=%s, runtime_seconds=%s)",
                OUTBOX_MAX_ENTRIES,
                (dropped.get("payload") or {}).get("sequence_number"),
                (dropped.get("payload") or {}).get("runtime_seconds"),
            )
        item = {
            "payload": payload,
            "attempts": 0,
            "next_attempt_at": time.time() + OUTBOX_BACKOFF_STEPS[0],
        }
        self.entries.append(item)
        return item

    def discard(self, item: dict) -> bool:
        """Remove an entry Core acknowledged; False if it is already gone."""
        for i, e in enumerate(self.entries):
            if e is item:
                del self.entries[i]
                return True
        return False

    def max_sequence(self) -> int:
        seqs = [
            int((e.get("payload") or {}).get("sequence_number") or 0)
            for e in self.entries
        ]
        return max(seqs, default=0)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version is None:
        entry.version = 1
    if entry.version == 1:
        data = {**entry.data}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated SmartFilterPro entry from v1 to v2")
    return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


def _is_climate_available(state) -> bool:
    """Check if climate entity is properly available."""
    if not state:
        return False
    return state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE, "unavailable", "unknown"}


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
    timestamp: Optional[datetime] = None,
    source_event_id: Optional[str] = None,
    tz_name: Optional[str] = None,
) -> dict:
    """
    Payload shape expected by Core Ingest (matches Hubitat 8-state format).
    Posted to the Core URL of the entry's environment (see const.ENVIRONMENTS).

    timestamp is the event time (default now). A runtime piece passes its
    end, because Core reads it as timestamp - runtime_seconds .. timestamp.
    """
    attrs = (state.attributes if state else None) or {}
    ts = (timestamp or _utcnow()).isoformat()

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
        # Home Assistant's configured zone. Core does not read it (it splits
        # days by the devices row); kept for debugging.
        "timezone": tz_name or "UTC",

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
        "source_event_id": source_event_id,

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

    # Post telemetry directly to Core (like Hubitat does), to the Core that
    # matches the Bubble environment this entry logged into. Entries from
    # before the environment selector only carry api_base; derive from that.
    core_ingest_url = entry.data.get(CONF_CORE_INGEST_URL) or core_ingest_url_for(
        entry.data.get(CONF_ENVIRONMENT), api_base
    )
    _LOGGER.info(
        "SFP environment=%s core=%s",
        entry.data.get(CONF_ENVIRONMENT) or "(derived from api_base)", core_ingest_url,
    )
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
    runtime_tracker.raise_sequence_floor(outbox.max_sequence())

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

    async def _send(payload: dict) -> None:
        """Number, log, then post one event to Core.

        Bridge-core's delivery order, ported: allocate the sequence number,
        write the event to the outbox and save the counter BEFORE the POST,
        and drop it from the outbox only once Core acknowledges it. If HA
        dies mid-POST, or the POST fails, the event is replayed unchanged by
        the drain (same sequence_number and source_event_id, so Core dedupes
        a copy it already has). _post_to_core has already done its own
        401-refresh retry by the time it returns False.
        """
        payload["sequence_number"] = runtime_tracker.get_and_increment_sequence()
        item = outbox.add(payload)
        await outbox.save()
        await runtime_tracker.save_state()
        if await _post_to_core(payload):
            if outbox.discard(item):
                await outbox.save()
            return
        _LOGGER.info(
            "SFP: Core POST failed; event kept in outbox for retry (sequence_number=%s, %d pending)",
            payload.get("sequence_number"), len(outbox.entries),
        )

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
                outbox.discard(item)
                changed = True
                _LOGGER.info(
                    "SFP: Outbox replay succeeded (sequence_number=%s, %d remaining)",
                    payload.get("sequence_number"), len(outbox.entries),
                )
                continue
            item["attempts"] = item.get("attempts", 0) + 1
            changed = True
            if item["attempts"] >= OUTBOX_MAX_ATTEMPTS:
                outbox.discard(item)
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

    # State changes, checkpoints and startup recovery all read and write the
    # same session; run them one at a time (each awaits network I/O).
    session_lock = asyncio.Lock()
    tz_name = getattr(getattr(hass, "config", None), "time_zone", None)

    def _device_kwargs(state) -> dict:
        return dict(
            user_id=user_id,
            hvac_id=hvac_id,
            entity_id=state.entity_id if state else climate_eid,
            device_name=state.name if state else None,
            thermostat_manufacturer=device_meta.get("manufacturer"),
            thermostat_model=device_meta.get("model"),
            tz_name=tz_name,
        )

    async def _close_session(state, at: datetime, reason: str, *, reachable: bool) -> None:
        """END the open session at `at`, carrying only runtime not yet posted.

        `at` is the last confirmation for a close HA could not confirm (the
        entity went unavailable, a restart, an hour without confirmation),
        so unconfirmed time is never counted.
        """
        rs = runtime_tracker.run_state
        previous_status = rs.get("last_equipment_status", "Idle")
        piece = take_piece(rs, at, allow_empty=True)
        clear_session(rs)
        if piece is None:
            return
        end_payload = _build_payload(
            state,
            hvac_mode=state.state if state else None,
            runtime_seconds=piece["runtime_seconds"],
            cycle_start=piece["session_start"].isoformat(),
            cycle_end=piece["end"].isoformat(),
            connected=reachable,
            is_reachable=reachable,
            event_type="Mode_Change",
            previous_status=previous_status,
            runtime_type="END",
            humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
            timestamp=piece["end"],
            source_event_id=piece_event_id(hvac_id, piece, "end"),
            **_device_kwargs(state),
        )
        await _send(end_payload)
        runtime_tracker.record_post(previous_status)
        _LOGGER.info(
            "SFP: Closed %s session (%s) at %s; final piece %ss",
            previous_status, reason, piece["end"].isoformat(), piece["runtime_seconds"],
        )

    async def _post_checkpoint(state, now: datetime) -> None:
        """Post the confirmed-running session's runtime since the last report."""
        rs = runtime_tracker.run_state
        piece = take_piece(rs, now)
        if piece is None:
            await runtime_tracker.save_state()
            return
        status = piece["status"]
        payload = _build_payload(
            state,
            hvac_mode=state.state,
            runtime_seconds=piece["runtime_seconds"],
            cycle_start=piece["session_start"].isoformat(),
            cycle_end=piece["end"].isoformat(),
            connected=True,
            is_reachable=True,
            event_type="Telemetry_Update",
            previous_status=status,
            runtime_type="CHECKPOINT",
            humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
            timestamp=piece["end"],
            source_event_id=piece_event_id(hvac_id, piece, "checkpoint"),
            **_device_kwargs(state),
        )
        await _send(payload)
        runtime_tracker.record_post(status)
        _LOGGER.debug("SFP: Checkpoint %s +%ss", status, piece["runtime_seconds"])

    async def _handle_state(new_state, *, end_at: Optional[datetime] = None,
                            start_at: Optional[datetime] = None) -> None:
        """Send payload on every climate state change; mark cycle start/stop.

        end_at/start_at say when a change took effect if the listener did not
        see it happen (the checkpoint timer or startup found it); live state
        changes take effect now.
        """
        rs = runtime_tracker.run_state
        now = _utcnow()
        if end_at is None and session_open(rs) and rs.get("resumed"):
            # First change seen for a session restored after a restart: what
            # ran while HA was down is unknown, so a different state ends the
            # old one at its last confirmation, not now.
            end_at, start_at = missed_change_times(rs, None, now)
        end_at = end_at or now
        start_at = start_at or now

        if not _is_climate_available(new_state):
            _LOGGER.debug("SFP: Device unavailable, posting is_reachable=false: %s",
                          new_state.state if new_state else "None")
            previous_status = rs.get("last_equipment_status", "Idle")

            # Nothing can confirm an open run while the entity is unavailable:
            # close it at its last confirmation, not now. If it comes back
            # running, that starts a new session.
            if session_open(rs):
                await _close_session(new_state, last_confirmed(rs), "device unavailable", reachable=False)

            # Post explicit offline/unreachable event so Core knows
            offline_payload = _build_payload(
                new_state,
                hvac_mode=new_state.state if new_state else None,
                connected=False,
                is_reachable=False,
                event_type="CONNECTIVITY_CHANGE",
                previous_status=previous_status,
                humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
                **_device_kwargs(new_state),
            )
            await _send(offline_payload)
            runtime_tracker.record_post("Idle")
            rs["last_is_reachable"] = False
            await runtime_tracker.save_state()
            return

        # Detect offline → online transition and send CONNECTIVITY_CHANGE
        was_reachable = rs.get("last_is_reachable")
        if was_reachable is False:
            _LOGGER.info("SFP: Device back online, sending CONNECTIVITY_CHANGE (is_reachable=true)")
            online_payload = _build_payload(
                new_state,
                hvac_mode=new_state.state,
                connected=True,
                is_reachable=True,
                event_type="CONNECTIVITY_CHANGE",
                previous_status=rs.get("last_equipment_status", "Idle"),
                humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
                **_device_kwargs(new_state),
            )
            await _send(online_payload)
            runtime_tracker.record_post("Idle")

        rs["last_is_reachable"] = True

        attrs = (new_state.attributes or {})
        hvac_mode = new_state.state  # Current thermostat mode (heat/cool/auto/off)
        hvac_action = attrs.get("hvac_action")
        classified_mode = _classify_mode(attrs)  # 'heating' | 'cooling' | 'fanonly' | 'idle'

        # Get 8-state equipment status for Core payload. Running means "not
        # Idle" — the same rule the payload uses, so a fan-only run (fan on
        # with hvac_action idle/off/missing) is tracked, not just labelled.
        equipment_status = _classify_8_state(attrs, hvac_mode)
        is_active = equipment_status != "Idle"

        # An open session nothing confirmed for over UNCONFIRMED_LIMIT (HA's
        # loop stalled, or restored after a long restart): close it at its
        # last confirmation. If it is running now, that is a new session.
        if unconfirmed_too_long(rs, now):
            await _close_session(new_state, last_confirmed(rs), "unconfirmed", reachable=True)
            end_at = start_at = now

        was_active = session_open(rs)
        if not was_active:
            clear_session(rs)
        previous_status = rs.get("last_equipment_status", "Idle")

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
            rs["last_active_mode"] = classified_mode

        payload = None

        # Pre-resolve humidity fallback once per state change so every payload
        # this handler emits sees the same value.
        humidity_fallback = _read_humidity_from_entity(hass, humidity_entity_id)

        common_kwargs = dict(
            connected=True,
            last_mode=rs.get("last_active_mode") if classified_mode == "idle" else classified_mode,
            is_reachable=True,
            previous_status=previous_status,
            humidity_fallback=humidity_fallback,
            **_device_kwargs(new_state),
        )

        if not was_active and is_active:
            # cycle start
            open_session(rs, start_at, equipment_status)
            payload = _build_payload(
                new_state,
                hvac_mode=hvac_mode,
                event_type="Mode_Change",
                timestamp=start_at,
                **common_kwargs,
            )
            _LOGGER.info(
                "SFP cycle start detected: action=%s fan_mode=%s equipment_status=%s",
                hvac_action, attrs.get("fan_mode"), equipment_status
            )

        elif was_active and not is_active:
            # cycle end: the END carries only the runtime since the last
            # checkpoint (see runtime.py)
            await _close_session(new_state, end_at, "cycle end", reachable=True)

        elif was_active and equipment_status != previous_status:
            # Active-to-active status change (e.g. Heating -> Heating_Fan):
            # END the old status with its unreported runtime (previous_status
            # = old), then open a session for the new one. Core joins the two
            # when they are the same mode.
            await _close_session(new_state, end_at, f"status change to {equipment_status}", reachable=True)
            open_session(rs, start_at, equipment_status)
            payload = _build_payload(
                new_state,
                hvac_mode=hvac_mode,
                event_type="Telemetry_Update",
                **common_kwargs,
            )

        else:
            # steady-state ping (telemetry update) — no status change. An
            # available entity still showing the same run confirms it.
            if was_active:
                confirm(rs, now)
            payload = _build_payload(
                new_state,
                hvac_mode=hvac_mode,
                event_type="Telemetry_Update",
                **common_kwargs,
            )

        # Update last equipment status for next comparison
        rs["last_equipment_status"] = equipment_status

        # Update last seen values
        rs["last_action"] = hvac_action

        # Save state after each change
        await runtime_tracker.save_state()

        if payload:
            event_type = payload.get("event_type", "Telemetry_Update")
            # Debounce: skip duplicate Telemetry_Update posts within 3 seconds
            if runtime_tracker.should_skip_duplicate_post(equipment_status, event_type):
                return

            await _send(payload)
            runtime_tracker.record_post(equipment_status)

    async def _check_session() -> None:
        """Confirm an open session with HA's current state (every CHECKPOINT_INTERVAL).

        - still running the same way: post a CHECKPOINT for the runtime since
          the last report;
        - stopped or changed (a change the listener never handled): handle
          it like that state change;
        - unavailable/unknown: not confirmed; past UNCONFIRMED_LIMIT, close
          at the last confirmation.
        """
        rs = runtime_tracker.run_state
        now = _utcnow()
        st = hass.states.get(climate_eid)

        if not _is_climate_available(st):
            if unconfirmed_too_long(rs, now):
                await _close_session(st, last_confirmed(rs), "unconfirmed", reachable=False)
                await runtime_tracker.save_state()
            return

        status = _classify_8_state(st.attributes or {}, st.state)
        running = status != "Idle"
        if (session_open(rs) and running and status == rs.get("last_equipment_status")
                and not unconfirmed_too_long(rs, now)):
            confirm(rs, now)
            await _post_checkpoint(st, now)
            return

        if session_open(rs) or running:
            end_at, start_at = missed_change_times(rs, getattr(st, "last_updated", None), now)
            _LOGGER.info(
                "SFP: Checkpoint found %s while tracking %s; applying the missed change",
                status, rs.get("last_equipment_status") if session_open(rs) else "Idle",
            )
            await _handle_state(st, end_at=end_at, start_at=start_at)

    async def _checkpoint_tick(_now=None) -> None:
        async with session_lock:
            try:
                await _check_session()
            except Exception:  # noqa: BLE001 — keep the timer alive
                _LOGGER.exception("SFP: runtime checkpoint failed")

    async def _on_change(event):
        new = event.data.get("new_state")
        if new and (not climate_eid or new.entity_id == climate_eid):
            async with session_lock:
                try:
                    await _handle_state(new)
                except Exception:  # noqa: BLE001 — one bad event must not become an unretrieved task error
                    _LOGGER.exception(
                        "SFP: state change handler failed for %s (state=%s); this event was not posted",
                        new.entity_id, new.state,
                    )

    async def _startup() -> None:
        """Recover a session saved before a restart, then prime an initial send.

        Resume it (checkpoints continue from "reported up to") if HA
        confirmed it within UNCONFIRMED_LIMIT; otherwise close it at its last
        confirmation. The old rule seeded a fresh start at "now" for anything
        older than an hour, losing everything before the restart.
        """
        rs = runtime_tracker.run_state
        now = _utcnow()
        st = hass.states.get(climate_eid)
        if session_open(rs):
            if unconfirmed_too_long(rs, now):
                await _close_session(st, last_confirmed(rs), "restart after more than an hour",
                                     reachable=_is_climate_available(st))
            else:
                rs["resumed"] = True
                _LOGGER.info(
                    "SFP: Resuming %s session from before the restart (reported up to %s)",
                    rs.get("last_equipment_status"), rs["reported_until"].isoformat(),
                )

        if st and _is_climate_available(st):
            # Seed reachability so the initial _handle_state doesn't
            # fire a spurious CONNECTIVITY_CHANGE on first boot
            if rs.get("last_is_reachable") is None:
                rs["last_is_reachable"] = True
            end_at, start_at = missed_change_times(rs, getattr(st, "last_updated", None), now)
            await _handle_state(st, end_at=end_at, start_at=start_at)
        else:
            # The checkpoint timer keeps checking; an open session is closed
            # at its last confirmation if the entity stays unavailable.
            await runtime_tracker.save_state()

    # Only watch telemetry if a climate entity was chosen in the flow
    unsub_telemetry = None
    unsub_checkpoint = None
    if climate_eid:
        _LOGGER.debug("SFP telemetry watching %s", climate_eid)
        unsub_telemetry = async_track_state_change_event(hass, [climate_eid], _on_change)
        unsub_checkpoint = async_track_time_interval(hass, _checkpoint_tick, CHECKPOINT_INTERVAL)
        async with session_lock:
            try:
                await _startup()
            except Exception:  # noqa: BLE001 — setup must still finish
                _LOGGER.exception("SFP: runtime recovery at startup failed")
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
                connected=_is_climate_available(s),
                last_mode=lm,
                is_reachable=_is_climate_available(s),
                event_type="Telemetry_Update",
                previous_status=previous_status,
                humidity_fallback=_read_humidity_from_entity(hass, humidity_entity_id),
                **_device_kwargs(s),
            )
            await _send(send_now_payload)

    hass.services.async_register(DOMAIN, "send_now", _svc_send_now)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        STORAGE_KEY: {
            "unsub_telemetry": unsub_telemetry,
            "runtime_tracker": runtime_tracker,
            "outbox": outbox,
            "unsub_outbox": unsub_outbox,
            "unsub_checkpoint": unsub_checkpoint,
        }
    }
    # No update listener on purpose. The entry's data is rewritten on every
    # token refresh (Bubble access token, Core token, the sensor's forced
    # refresh), and a reload-on-update listener turned each of those into a
    # full reload: every entity went unavailable and came back, the logbook
    # recorded the button as "Pressed" each time it was re-created, and the
    # in-memory runtime tracker and outbox were torn down mid-cycle. There is
    # no options flow, so nothing legitimately needs a reload; every reader
    # already re-fetches entry.data live.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data and STORAGE_KEY in data:
        unsub = data[STORAGE_KEY].get("unsub_telemetry")
        if unsub:
            try:
                unsub()
            except Exception:
                pass

        for key in ("unsub_outbox", "unsub_checkpoint"):
            unsub_timer = data[STORAGE_KEY].get(key)
            if unsub_timer:
                try:
                    unsub_timer()
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
