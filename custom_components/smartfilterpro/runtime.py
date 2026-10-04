"""Runtime sessions and Core checkpoints, with no Home Assistant imports.

Core creates runtime only from events with runtime_seconds > 0, covering
``timestamp - runtime_seconds .. timestamp`` for the status in
previous_status, and rejects any single piece over 24 hours. Reporting a run
as one total when it ended therefore lost every run longer than a day (and
this integration also clamped totals to 86400 s, trimming a 60-hour fan run
to 24 hours). Instead, while equipment keeps running the integration posts
the runtime since its previous report every CHECKPOINT_INTERVAL
(runtime_type "CHECKPOINT"), and an END carries only what is left. Pieces of
one run are contiguous whole seconds, so Core joins them into one session
and splits it at the device's local midnight.

This is a Python port of the checkpoint semantics the Node bridges share
through @smartfilterpro/bridge-core (SmartThings routes/st-webhook.js:
postCheckpoint / checkpointDevice / closeSessionAt; Nest
src/services/runtimeTracker.js: postCheckpoint / recoverActiveSessions).
Only time that was confirmed is counted: a session nothing has confirmed for
UNCONFIRMED_LIMIT is closed at its last confirmation, never at "now".

Everything here works on the RuntimeTracker.run_state dict so the same
fields are what gets persisted across restarts.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# Post the runtime since the last report this often while a run continues.
CHECKPOINT_INTERVAL = timedelta(minutes=15)

# A session nothing has confirmed for longer than this is closed at its last
# confirmation; the next confirmation that shows it running starts a new one.
UNCONFIRMED_LIMIT = timedelta(hours=1)

# hvac_action values that mean equipment is running
ACTIVE_ACTIONS = {"heating", "cooling", "fan"}

# fan_mode values that mean the blower is moving air on its own
FAN_ACTIVE_MODES = {"on", "on_high", "circulate"}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_8_state(attrs: dict, hvac_mode: Optional[str] = None) -> str:
    """Classify thermostat state using the 8-state system (matches Hubitat).

    Cooling_Fan, Cooling, Heating_Fan, Heating, AuxHeat_Fan, AuxHeat,
    Fan_only, Idle.

    hvac_action says what equipment is running; fan_mode says whether the
    fan is set to run on its own. A fan set to on/circulate moves air
    whatever hvac_action says (idle, off, or not reported at all: Nest's fan
    timer reports hvac_action "off" with fan_mode "on"), so that is
    Fan_only. This is the ONLY place that decides whether the system is
    running: the runtime tracker and the payload use it alike.
    """
    if not attrs:
        return "Idle"

    hvac_action = (attrs.get("hvac_action") or "idle").lower()
    fan_mode = (attrs.get("fan_mode") or "auto")
    fan_mode = fan_mode.strip().lower() if isinstance(fan_mode, str) else "auto"
    preset_mode = (attrs.get("preset_mode") or "").lower()
    hvac_mode_attr = (attrs.get("hvac_mode") or hvac_mode or "").lower()

    cooling_active = hvac_action == "cooling"
    heating_active = hvac_action == "heating"
    fan_explicitly_on = fan_mode in FAN_ACTIVE_MODES
    fan_only_mode = hvac_action == "fan"

    # Auxiliary/emergency heat may show in preset_mode or hvac_mode
    is_aux_heat = (
        "emergency" in preset_mode or
        "aux" in preset_mode or
        "emergency" in hvac_mode_attr or
        hvac_mode_attr == "heat_cool" and "aux" in hvac_action
    )

    if is_aux_heat and heating_active and fan_explicitly_on:
        return "AuxHeat_Fan"
    if is_aux_heat and heating_active:
        return "AuxHeat"
    if cooling_active and fan_explicitly_on:
        return "Cooling_Fan"
    if cooling_active:
        return "Cooling"
    if heating_active and fan_explicitly_on:
        return "Heating_Fan"
    if heating_active:
        return "Heating"
    if fan_only_mode or fan_explicitly_on:
        return "Fan_only"
    return "Idle"


def attrs_is_active(attrs: dict, hvac_mode: Optional[str] = None) -> bool:
    """True when the system is moving air (anything but Idle)."""
    return classify_8_state(attrs, hvac_mode) != "Idle"


def classify_mode(attrs: dict) -> str:
    """Return one of 'heating', 'cooling', 'fanonly', 'idle'."""
    status = classify_8_state(attrs)
    if status.startswith("Cooling"):
        return "cooling"
    if status.startswith("Heating") or status.startswith("AuxHeat"):
        return "heating"
    if status == "Fan_only":
        return "fanonly"
    return "idle"


# ---------------------------------------------------------------------------
# Session state (keys of RuntimeTracker.run_state)
#   is_active             a session is open
#   active_since          when the open session (this status) started
#   reported_until        runtime has been posted up to here
#   last_confirmed        last time HA showed it running this way
#   last_equipment_status the status that is running
# ---------------------------------------------------------------------------

def session_open(rs: dict) -> bool:
    """A session is open only with a start time; is_active alone is not one."""
    return bool(rs.get("is_active")) and rs.get("active_since") is not None


def open_session(rs: dict, at: datetime, status: str) -> None:
    rs["is_active"] = True
    rs["active_since"] = at
    rs["reported_until"] = at
    rs["last_confirmed"] = at
    rs["last_equipment_status"] = status
    rs["resumed"] = False


def clear_session(rs: dict) -> None:
    rs["is_active"] = False
    rs["active_since"] = None
    rs["reported_until"] = None
    rs["last_confirmed"] = None
    rs["resumed"] = False


def last_confirmed(rs: dict) -> Optional[datetime]:
    """Latest of the session start, the last report and the last confirmation."""
    times = [t for t in (rs.get("active_since"), rs.get("reported_until"), rs.get("last_confirmed")) if t]
    return max(times) if times else None


def confirm(rs: dict, at: datetime) -> None:
    """Record that HA showed the open session still running at `at`."""
    current = last_confirmed(rs)
    if current is None or at > current:
        rs["last_confirmed"] = at
    rs["resumed"] = False


def unconfirmed_too_long(rs: dict, now: datetime) -> bool:
    lc = last_confirmed(rs)
    return session_open(rs) and lc is not None and now - lc > UNCONFIRMED_LIMIT


def take_piece(rs: dict, until: datetime, *, allow_empty: bool = False) -> Optional[dict]:
    """The runtime from reported_until to `until`, in whole seconds.

    Advances reported_until by exactly that many seconds, so consecutive
    pieces are contiguous and Core joins them. Returns None when there is
    nothing to report, unless allow_empty (an END still marks the stop).
    """
    start = rs.get("active_since")
    frm = rs.get("reported_until") or start
    if start is None or frm is None:
        return None
    secs = int((until - frm).total_seconds())
    if secs <= 0:
        if not allow_empty:
            return None
        secs = 0
    to = frm + timedelta(seconds=secs)
    rs["reported_until"] = to
    return {
        "session_start": start,
        "start": frm,
        "end": to,
        "runtime_seconds": secs,
        "status": rs.get("last_equipment_status") or "Idle",
    }


def piece_event_id(device_id: str, piece: dict, kind: str) -> str:
    """Deterministic source_event_id for a runtime piece.

    Derived from the session and the piece's start, so a retry (or a piece
    rebuilt after a crash) dedupes in Core on (device_id, source_event_id).
    """
    return f"{device_id}:{piece['session_start'].isoformat()}:{kind}:{piece['start'].isoformat()}"


def missed_change_times(
    rs: dict, last_updated: Optional[datetime], now: datetime
) -> tuple[datetime, datetime]:
    """When a change the listener did not handle took effect.

    Returns (end_at, start_at): when the open session's status stopped, and
    when a new one began.
      - A session restored after a restart and not confirmed since: what
        happened while HA was down is unknown, so the old status ends at its
        last confirmation and a new one starts now.
      - Otherwise HA's own record of when the entity last changed
        (state.last_updated), kept between the last confirmation and now.
      - No open session: a start found late begins now.
    """
    if not session_open(rs):
        return now, now
    lc = last_confirmed(rs) or now
    if rs.get("resumed"):
        return lc, now
    at = last_updated or now
    at = min(max(at, lc), now)
    return at, at


def parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
