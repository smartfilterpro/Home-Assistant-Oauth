"""Runtime checkpoint tests, run without Home Assistant installed.

The Home Assistant modules __init__.py imports are replaced with stubs (a
fake hass with a state machine, Store, timers and an HTTP session that
records what would be posted to Core), and the clock is simulated, so these
drive the real async_setup_entry, state-change listener and checkpoint timer
through multi-day scenarios in well under a second:

  a. a 60-hour fan run (Mon 00:00 to Wed 12:00 America/New_York) is posted
     as contiguous checkpoints of at most 15 minutes plus a small END,
     totalling exactly 216000 s (it used to be one END clamped to 86400);
  b. a stop only the 15-minute confirmation read sees is ENDed when HA
     recorded it;
  c. a run nothing confirms for over an hour is closed at its last
     confirmation (no unconfirmed time) and a later run is a new session;
  d. a restart mid-run resumes from "reported up to";
  e. a restart after more than an hour closes at the last confirmation;
  f. bugs fixed here: a restart while the entity is unavailable lost the
     whole run (and a status change then sent runtime_seconds=None); a fan
     run with hvac_action off/None was "Fan_only" in the payload but idle to
     the tracker; a sequence number was reused after every restart; an event
     in flight when HA died was lost; a full outbox could drop runtime
     before telemetry; the timezone was always "UTC".

    python scripts/test_runtime_checkpoints.py
    python scripts/test_runtime_checkpoints.py --dump run.json   # 60 h payloads for Core
"""
import asyncio
import copy
import importlib.util
import json
import logging
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "smartfilterpro"

# --- Home Assistant stubs ----------------------------------------------------


def _module(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


ha = _module("homeassistant")
ha_core = _module("homeassistant.core")
ha_ce = _module("homeassistant.config_entries")
ha_const = _module("homeassistant.const")
ha_helpers = _module("homeassistant.helpers")
ha_event = _module("homeassistant.helpers.event")
ha_aio = _module("homeassistant.helpers.aiohttp_client")
ha_cv = _module("homeassistant.helpers.config_validation")
ha_er = _module("homeassistant.helpers.entity_registry")
ha_dr = _module("homeassistant.helpers.device_registry")
ha_storage = _module("homeassistant.helpers.storage")
ha.helpers = ha_helpers
ha_helpers.config_validation = ha_cv
ha_helpers.entity_registry = ha_er
ha_helpers.device_registry = ha_dr
try:
    import aiohttp  # noqa: F401  (auth.py imports it; nothing here calls it)
except ImportError:
    _module("aiohttp")


class HomeAssistant:  # noqa: D101
    pass


class ConfigEntry:  # noqa: D101
    pass


class _Registry:
    def async_get(self, _id):
        return None


ha_core.HomeAssistant = HomeAssistant
ha_core.callback = lambda f: f
ha_ce.ConfigEntry = ConfigEntry
ha_const.STATE_UNKNOWN = "unknown"
ha_const.STATE_UNAVAILABLE = "unavailable"
ha_cv.config_entry_only_config_schema = lambda domain: None
ha_er.async_get = lambda hass: _Registry()
ha_er.async_entries_for_device = lambda *a, **k: []
ha_dr.async_get = lambda hass: _Registry()
ha_event.async_track_state_change_event = lambda hass, ids, cb: hass.track_state(ids, cb)
ha_event.async_track_time_interval = lambda hass, cb, interval: hass.track_interval(cb, interval)
ha_aio.async_get_clientsession = lambda hass: hass.session


class Store:
    """HA Store backed by the fake hass's dict, JSON round-tripped like disk."""

    def __init__(self, hass, version, key):
        self._disk = hass.storage
        self._key = key

    async def async_load(self):
        raw = self._disk.get(self._key)
        return json.loads(raw) if raw is not None else None

    async def async_save(self, data):
        self._disk[self._key] = json.dumps(data)


ha_storage.Store = Store

# --- simulated clock -----------------------------------------------------------


class Clock:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return Clock.now if tz is None else Clock.now.astimezone(tz)


# --- load the integration as a package -----------------------------------------
spec = importlib.util.spec_from_file_location(
    "sfp", COMPONENT / "__init__.py", submodule_search_locations=[str(COMPONENT)]
)
sfp = importlib.util.module_from_spec(spec)
sys.modules["sfp"] = sfp
spec.loader.exec_module(sfp)
sfp.datetime = FakeDatetime
# The scenarios fail Core on purpose; keep the integration's error logs out
# of the test output.
logging.getLogger("sfp").setLevel(logging.CRITICAL)
sfp.time = types.SimpleNamespace(time=lambda: Clock.now.timestamp())


class FakeAuth:
    def __init__(self, hass, entry):
        pass

    async def ensure_core_token_valid(self):
        return "test-core-token"

    async def _issue_core_token(self):
        return "test-core-token"


sfp.SfpAuth = FakeAuth

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}{(': ' + str(detail)) if detail else ''}")


# --- fakes ---------------------------------------------------------------------
ENTITY = "climate.hallway"
DEVICE_ID = "e2e-home-assistant"
NY = "America/New_York"


class Crash(BaseException):
    """HA dying mid-POST (not an Exception, so nothing in the code catches it)."""


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def text(self):
        return '{"ok":true}' if self.status == 200 else "error"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Records every event Core acknowledged; can fail or 'crash' instead."""

    def __init__(self, sink):
        self.sink = sink
        self.mode = "ok"

    def post(self, url, json=None, headers=None, timeout=None):
        if self.mode == "crash":
            raise Crash()
        if self.mode == "fail":
            return FakeResponse(503)
        for ev in json:
            self.sink.append(copy.deepcopy(ev))
        return FakeResponse(200)


class FakeState:
    def __init__(self, state, attributes):
        self.entity_id = ENTITY
        self.state = state
        self.attributes = attributes
        self.name = "Hallway"
        self.last_updated = Clock.now


class FakeEvent:
    def __init__(self, new_state):
        self.data = {"new_state": new_state}


class FakeEntry:
    entry_id = "entry-1"
    version = 2
    data = {
        "api_base": "https://smartfilterpro.com",
        "user_id": "user-e2e",
        "hvac_id": DEVICE_ID,
        "climate_entity_id": ENTITY,
        "core_ingest_url": "http://core.invalid/ingest/v1/events:batch",
    }


async def _noop(*a, **k):
    return True


class FakeHass:
    def __init__(self, storage, sink):
        self.storage = storage
        self.states = {}
        self.data = {}
        self.config = types.SimpleNamespace(time_zone=NY)
        self.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=_noop,
            async_get_entry=lambda _id: FakeEntry,
            async_update_entry=lambda *a, **k: None,
        )
        self.services = types.SimpleNamespace(async_register=lambda *a, **k: None)
        self.session = FakeSession(sink)
        self.listeners = []
        self.timers = []

    def track_state(self, ids, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def track_interval(self, cb, interval):
        timer = [cb, interval, Clock.now + interval]
        self.timers.append(timer)
        return lambda: self.timers.remove(timer)


class Sim:
    """One Home Assistant process; restart() starts another on the same disk."""

    def __init__(self, at):
        Clock.now = at
        self.storage = {}
        self.posts = []
        self.hass = None
        self.state = None

    async def start(self, state=None):
        self.hass = FakeHass(self.storage, self.posts)
        if state is not None:
            self.state = state
        if self.state is not None:
            self.hass.states[ENTITY] = self.state
        await sfp.async_setup_entry(self.hass, FakeEntry)

    async def restart(self, at, state):
        """HA died (no unload, no final save) and comes back at `at`."""
        self.hass = None
        Clock.now = at
        st = FakeState(*state) if isinstance(state, tuple) else state
        await self.start(st)

    async def set(self, state, attrs=None, fire=True):
        st = FakeState(state, dict(attrs or {}))
        self.state = st
        self.hass.states[ENTITY] = st
        if fire:
            for cb in list(self.hass.listeners):
                await cb(FakeEvent(st))

    async def run_until(self, at):
        while True:
            due = [t for t in self.hass.timers if t[2] <= at]
            if not due:
                break
            timer = min(due, key=lambda t: t[2])
            Clock.now = timer[2]
            timer[2] = timer[2] + timer[1]
            await timer[0](Clock.now)
        Clock.now = at


def ny(day, hh, mm=0, ss=0):
    """A wall-clock time in New York (EDT, UTC-4, in October 2026) as UTC."""
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)


IDLE = ("heat", {"hvac_action": "idle", "fan_mode": "auto"})
HEAT = ("heat", {"hvac_action": "heating", "fan_mode": "auto"})
HEAT_FAN = ("heat", {"hvac_action": "heating", "fan_mode": "on"})
COOL = ("cool", {"hvac_action": "cooling", "fan_mode": "auto"})
FAN = ("heat", {"hvac_action": "fan", "fan_mode": "on"})


def ts(p):
    return datetime.fromisoformat(p["timestamp"])


def pieces(posts):
    """Runtime pieces, the only events Core creates runtime from."""
    return [p for p in posts if isinstance(p.get("runtime_seconds"), int) and p["runtime_seconds"] > 0]


def total(posts):
    return sum(p["runtime_seconds"] for p in pieces(posts))


def core_mode(status):
    s = (status or "").upper()
    for key, mode in (("AUX", "auxheat"), ("HEAT", "heat"), ("COOL", "cool"), ("FAN", "fan")):
        if key in s:
            return mode
    return None


def core_sessions(posts):
    """Core's joining rule: same mode, next piece starts within 5 min of the last."""
    out = []
    for p in pieces(posts):
        mode = core_mode(p.get("previous_status") or p.get("equipment_status"))
        start = ts(p) - timedelta(seconds=p["runtime_seconds"])
        last = out[-1] if out else None
        if last and last["mode"] == mode and abs((start - last["end"]).total_seconds()) <= 300:
            last["end"] = ts(p)
            last["seconds"] += p["runtime_seconds"]
        else:
            out.append({"mode": mode, "start": start, "end": ts(p), "seconds": p["runtime_seconds"]})
    return out


def contiguous(posts):
    ps = pieces(posts)
    for a, b in zip(ps, ps[1:]):
        if ts(b) - timedelta(seconds=b["runtime_seconds"]) != ts(a):
            return False
    return True


def seqs_increase(posts):
    s = [p.get("sequence_number") for p in posts]
    return all(isinstance(x, int) for x in s) and all(b > a for a, b in zip(s, s[1:]))


# --- scenarios -----------------------------------------------------------------

async def scenario_60h(dump_path=None):
    print("\n(a) 60-hour fan run, Mon 00:00 to Wed 12:00 New York")
    sim = Sim(ny(4, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(5, 0)
    await sim.set(*FAN)
    await sim.run_until(ny(7, 12))
    await sim.set(*IDLE)
    posts = sim.posts

    ps = pieces(posts)
    ckpts = [p for p in ps if p.get("runtime_type") == "CHECKPOINT"]
    ends = [p for p in posts if p.get("runtime_type") == "END"]
    check("runtime totals exactly 216000 s", total(posts) == 216000, total(posts))
    check("240 checkpoints plus one END", len(ckpts) == 240 and len(ends) == 1, (len(ckpts), len(ends)))
    check("every piece is at most 15 minutes", all(p["runtime_seconds"] <= 900 for p in ps),
          max((p["runtime_seconds"] for p in ps), default=None))
    check("the END carries only the runtime since the last checkpoint",
          len(ends) == 1 and ends[0]["runtime_seconds"] == 450, ends and ends[0]["runtime_seconds"])
    check("pieces are contiguous whole seconds", contiguous(posts))
    sessions = core_sessions(posts)
    check("Core joins them into one fan session", len(sessions) == 1 and sessions[0]["mode"] == "fan"
          and sessions[0]["start"] == ny(5, 0) and sessions[0]["end"] == ny(7, 12), sessions)
    good_fields = all(
        p["event_type"] == "Telemetry_Update" and p["is_active"] is True
        and p["equipment_status"] == "Fan_only" and p["previous_status"] == "Fan_only"
        for p in ckpts
    )
    check("checkpoints: Telemetry_Update, is_active, status in equipment_status and previous_status",
          bool(ckpts) and good_fields)
    ids = [p.get("source_event_id") for p in ps]
    check("every piece has a unique deterministic source_event_id",
          all(ids) and len(set(ids)) == len(ids)
          and ids[0] == f"{DEVICE_ID}:{ny(5, 0).isoformat()}:checkpoint:{ny(5, 0).isoformat()}", ids[:1])
    check("sequence numbers strictly increase", seqs_increase(posts))
    if dump_path:
        pathlib.Path(dump_path).write_text(json.dumps(posts, indent=1))
        print(f"  wrote {len(posts)} payloads to {dump_path}")


async def scenario_missed_stop():
    print("\n(b) a stop only the confirmation read sees")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 1, 3))
    await sim.set(*IDLE, fire=False)  # the listener never sees this change
    await sim.run_until(ny(6, 1, 30))
    ends = [p for p in sim.posts if p.get("runtime_type") == "END"]
    check("the checkpoint read ENDs the run", len(ends) == 1, len(ends))
    check("at the time HA recorded the stop (01:03)", bool(ends) and ts(ends[0]) == ny(6, 1, 3),
          ends and ends[0]["timestamp"])
    check("runtime is 3780 s, nothing after the stop", total(sim.posts) == 3780, total(sim.posts))
    check("pieces are contiguous", contiguous(sim.posts))


async def scenario_unconfirmed():
    print("\n(c1) entity unavailable mid-run: closed at the last confirmation")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 0, 50))  # confirmed at 00:07:30, 00:22:30, 00:37:30
    await sim.set("unavailable", {})
    await sim.run_until(ny(6, 3))
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 4))
    await sim.set(*IDLE)
    sessions = core_sessions(sim.posts)
    check("first session ends at the last confirmation (00:37:30)",
          len(sessions) == 2 and sessions[0]["end"] == ny(6, 0, 37, 30), sessions)
    check("the run after it is a new session from 03:00",
          len(sessions) == 2 and sessions[1]["start"] == ny(6, 3) and sessions[1]["end"] == ny(6, 4), sessions)
    check("no unconfirmed time: 2250 + 3600 s", total(sim.posts) == 5850, total(sim.posts))
    check("CONNECTIVITY_CHANGE offline still posted",
          any(p["event_type"] == "CONNECTIVITY_CHANGE" and p["is_reachable"] is False for p in sim.posts))
    check("sequence numbers strictly increase", seqs_increase(sim.posts))

    print("\n(c2) nothing confirms the run for over an hour (no state event at all)")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 0, 50))
    await sim.set("unavailable", {}, fire=False)
    await sim.run_until(ny(6, 2, 30))
    closed = [p for p in sim.posts if p.get("runtime_type") == "END"]
    check("closed once the hour is up, at the last confirmation",
          len(closed) == 1 and ts(closed[0]) == ny(6, 0, 37, 30), closed and closed[0]["timestamp"])
    await sim.set(*HEAT, fire=False)
    await sim.run_until(ny(6, 3))
    await sim.set(*IDLE)
    sessions = core_sessions(sim.posts)
    check("running again is a new session, started when confirmed (02:37:30)",
          len(sessions) == 2 and sessions[1]["start"] == ny(6, 2, 37, 30), sessions)
    check("no unconfirmed time: 2250 + 1350 s", total(sim.posts) == 3600, total(sim.posts))


async def scenario_restart_resume():
    print("\n(d) restart mid-run, back within the hour: resume from the last report")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 0, 40))
    before = [p["sequence_number"] for p in sim.posts]
    await sim.restart(ny(6, 0, 55), HEAT)
    after = [p["sequence_number"] for p in sim.posts[len(before):]]
    await sim.run_until(ny(6, 2))
    await sim.set(*IDLE)
    sessions = core_sessions(sim.posts)
    check("one session 00:00-02:00, 7200 s", len(sessions) == 1 and sessions[0]["seconds"] == 7200
          and sessions[0]["start"] == ny(6, 0) and sessions[0]["end"] == ny(6, 2), sessions)
    check("checkpoints continue from 'reported up to' after the restart",
          any(p.get("runtime_type") == "CHECKPOINT"
              and ts(p) - timedelta(seconds=p["runtime_seconds"]) == ny(6, 0, 37, 30)
              for p in sim.posts), [(p["timestamp"], p["runtime_seconds"]) for p in pieces(sim.posts)][:6])
    check("pieces are contiguous", contiguous(sim.posts))
    check("no sequence number is reused after the restart (f)",
          bool(after) and min(after) > max(before), (max(before), after[:1]))


async def scenario_restart_stale():
    print("\n(e) restart after more than an hour down: close at the last confirmation")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 0, 40))
    await sim.restart(ny(6, 2, 30), HEAT)
    await sim.run_until(ny(6, 3))
    await sim.set(*IDLE)
    sessions = core_sessions(sim.posts)
    check("the run before the restart is kept, ending at 00:37:30",
          len(sessions) == 2 and sessions[0]["start"] == ny(6, 0) and sessions[0]["end"] == ny(6, 0, 37, 30),
          sessions)
    check("the run seen after the restart is a new session from 02:30",
          len(sessions) == 2 and sessions[1]["start"] == ny(6, 2, 30) and sessions[1]["seconds"] == 1800, sessions)
    check("no time while HA was down is counted: 2250 + 1800 s", total(sim.posts) == 4050, total(sim.posts))


async def scenario_restart_unavailable():
    print("\n(f1) restart with the entity still unavailable, then a status change")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 0, 40))
    await sim.restart(ny(6, 3), ("unavailable", {}))
    await sim.run_until(ny(6, 3, 5))
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 4, 5))  # last checkpoint 04:00
    await sim.set(*HEAT_FAN)
    await sim.run_until(ny(6, 5))
    await sim.set(*IDLE)
    mode_ends = [p for p in sim.posts if p.get("runtime_type") == "END"]
    check("every END carries whole seconds (never None)",
          bool(mode_ends) and all(isinstance(p["runtime_seconds"], int) for p in mode_ends),
          [p["runtime_seconds"] for p in mode_ends])
    status_end = [p for p in mode_ends if p.get("previous_status") == "Heating" and p["equipment_status"] == "Heating_Fan"]
    check("the Heating -> Heating_Fan change ENDs Heating with its unreported runtime",
          len(status_end) == 1 and status_end[0]["runtime_seconds"] == 300 and ts(status_end[0]) == ny(6, 4, 5),
          [(p["timestamp"], p["runtime_seconds"]) for p in status_end])
    sessions = core_sessions(sim.posts)
    check("before the restart: 00:00-00:37:30", bool(sessions) and sessions[0]["end"] == ny(6, 0, 37, 30), sessions)
    check("after it: one heat session 03:05-05:00 (6900 s)",
          len(sessions) == 2 and sessions[1]["start"] == ny(6, 3, 5) and sessions[1]["seconds"] == 6900, sessions)
    check("total 2250 + 6900 s", total(sim.posts) == 9150, total(sim.posts))


async def scenario_fan_only():
    for label, attrs in (("off", {"hvac_action": "off", "fan_mode": "on"}),
                         ("None", {"fan_mode": "on"})):
        print(f"\n(f2) fan run with hvac_action {label} and fan_mode on")
        sim = Sim(ny(5, 23, 52, 30))
        await sim.start(FakeState("off", {"hvac_action": "off", "fan_mode": "auto"}))
        Clock.now = ny(6, 0)
        await sim.set("off", attrs)
        await sim.run_until(ny(6, 3))
        await sim.set("off", {"hvac_action": "off", "fan_mode": "auto"})
        during = [p for p in sim.posts if ny(6, 0) <= ts(p) < ny(6, 3)]
        check("the payload says Fan_only / active while it runs",
              bool(during) and all(p["equipment_status"] == "Fan_only" and p["is_active"] for p in during))
        check("and the tracker counts it: 10800 s of fan", total(sim.posts) == 10800
              and all(core_mode(p["previous_status"]) == "fan" for p in pieces(sim.posts)), total(sim.posts))


async def scenario_crash_mid_post():
    print("\n(f3) HA dies while a START is being posted")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    sim.hass.session.mode = "crash"
    try:
        await sim.set(*HEAT)
    except Crash:
        pass
    pending = json.loads(sim.storage.get("smartfilterpro_entry-1_outbox") or '{"entries": []}')["entries"]
    check("the event was in the outbox before the POST", len(pending) == 1
          and pending[0]["payload"]["event_type"] == "Mode_Change", len(pending))
    await sim.restart(ny(6, 0, 5), HEAT)
    await sim.run_until(ny(6, 0, 8))
    seqs = [p["sequence_number"] for p in sim.posts]
    delivered = [p for p in sim.posts if pending and p["sequence_number"] == pending[0]["payload"]["sequence_number"]]
    check("it is replayed, unchanged, after the restart", len(delivered) == 1, seqs)
    check("no sequence number is used twice", len(seqs) == len(set(seqs)), seqs)


async def scenario_core_outage():
    print("\n(f5) Core down for 2 hours mid-run, with enough telemetry to overflow the outbox")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 1))
    sim.hass.session.mode = "fail"
    t = ny(6, 1)
    for i in range(150):  # a temperature change every 48 s
        t += timedelta(seconds=48)
        await sim.run_until(t)
        await sim.set("heat", {"hvac_action": "heating", "fan_mode": "auto", "current_temperature": 60 + i % 7})
    await sim.run_until(ny(6, 3))
    sim.hass.session.mode = "ok"
    await sim.run_until(ny(6, 4))
    await sim.set(*IDLE)
    await sim.run_until(ny(6, 4, 30))
    sessions = core_sessions(sorted(sim.posts, key=lambda p: p["sequence_number"]))
    check("every runtime piece survives the outage: one 4-hour session",
          len(sessions) == 1 and sessions[0]["seconds"] == 14400, sessions)
    seqs = [p["sequence_number"] for p in sim.posts]
    check("replays reuse their sequence numbers (none sent twice)", len(seqs) == len(set(seqs)))


async def scenario_timezone():
    print("\n(f4) payload timezone")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    check("is Home Assistant's configured zone", bool(sim.posts) and sim.posts[0]["timezone"] == NY,
          sim.posts and sim.posts[0]["timezone"])


async def scenario_mode_switch():
    print("\nmode switch mid-run: END the old mode, checkpoints for the new one")
    sim = Sim(ny(5, 23, 52, 30))
    await sim.start(FakeState(*IDLE))
    Clock.now = ny(6, 0)
    await sim.set(*HEAT)
    await sim.run_until(ny(6, 0, 20))
    await sim.set(*COOL)
    await sim.run_until(ny(6, 0, 50))
    await sim.set(*IDLE)
    sessions = core_sessions(sim.posts)
    check("heat 1200 s then cool 1800 s",
          [(s["mode"], s["seconds"]) for s in sessions] == [("heat", 1200), ("cool", 1800)], sessions)
    heat_end = [p for p in sim.posts if p.get("runtime_type") == "END" and p.get("previous_status") == "Heating"]
    check("the heat END carries only 00:07:30-00:20 (750 s)",
          len(heat_end) == 1 and heat_end[0]["runtime_seconds"] == 750, [p["runtime_seconds"] for p in heat_end])
    check("cool has checkpoints", any(p.get("runtime_type") == "CHECKPOINT" and p["previous_status"] == "Cooling"
                                      for p in sim.posts))


async def main():
    dump = None
    if "--dump" in sys.argv:
        dump = sys.argv[sys.argv.index("--dump") + 1]
    for scenario in (
        lambda: scenario_60h(dump),
        scenario_missed_stop,
        scenario_unconfirmed,
        scenario_restart_resume,
        scenario_restart_stale,
        scenario_restart_unavailable,
        scenario_fan_only,
        scenario_crash_mid_post,
        scenario_core_outage,
        scenario_timezone,
        scenario_mode_switch,
    ):
        try:
            await scenario()
        except Exception as e:  # noqa: BLE001 — report and keep going
            check("scenario ran", False, f"{type(e).__name__}: {e}")


asyncio.run(main())
if failures == 0:
    print("\nAll checks passed")
    sys.exit(0)
print(f"\n{failures} check(s) failed")
sys.exit(1)
