"""Tests for auth.py's token handling, run without Home Assistant installed.

The two Home Assistant modules auth.py imports are replaced with stubs, and
aiohttp.ClientSession is replaced with a scripted fake, so this exercises
the real code paths that failed on a live hub:

  - a Bubble "soft 401" (HTTP 200, body carrying status 401) while issuing
    a core_token must refresh the access token and retry once;
  - a workflow that returns {} must fail clearly without refreshing;
  - epoch values in milliseconds must be read as seconds.

    python scripts/test_auth.py
"""
import asyncio
import importlib.util
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "smartfilterpro"

# --- Home Assistant stubs ----------------------------------------------------
ha = types.ModuleType("homeassistant")
ha_core = types.ModuleType("homeassistant.core")
ha_ce = types.ModuleType("homeassistant.config_entries")


class HomeAssistant:  # noqa: D101
    pass


class ConfigEntry:  # noqa: D101
    pass


ha_core.HomeAssistant = HomeAssistant
ha_ce.ConfigEntry = ConfigEntry
sys.modules.update({"homeassistant": ha, "homeassistant.core": ha_core, "homeassistant.config_entries": ha_ce})

# Load const.py and auth.py as a package so auth's relative import resolves.
pkg = types.ModuleType("sfp")
pkg.__path__ = [str(COMPONENT)]
sys.modules["sfp"] = pkg


def load(name):
    spec = importlib.util.spec_from_file_location(f"sfp.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"sfp.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


const = load("const")
auth = load("auth")

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}{(': ' + detail) if detail else ''}")


# --- fakes -------------------------------------------------------------------
class FakeEntry:
    def __init__(self, data):
        self.entry_id = "e1"
        self.data = dict(data)


class FakeConfigEntries:
    def __init__(self, entry):
        self.entry = entry
        self.updates = 0

    def async_update_entry(self, entry, data=None, **_):
        self.updates += 1
        self.entry.data = dict(data)

    def async_get_entry(self, _id):
        return self.entry


class FakeHass:
    def __init__(self, entry):
        self.config_entries = FakeConfigEntries(entry)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Scripted responses keyed by URL path suffix, consumed in order."""
    calls = []
    script = {}

    def post(self, url, json=None, headers=None, timeout=None):
        key = url.rsplit("/", 1)[-1]
        FakeSession.calls.append((key, headers or {}, json))
        queue = FakeSession.script.get(key) or []
        if not queue:
            raise AssertionError(f"unexpected call to {key}")
        status, body = queue.pop(0)
        return FakeResponse(status, body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


auth.aiohttp.ClientSession = FakeSession

BASE = {
    const.CONF_API_BASE: "https://smartfilterpro.com/version-test",
    const.CONF_ACCESS_TOKEN: "at-old",
    const.CONF_REFRESH_TOKEN: "rt-old",
    const.CONF_EXPIRES_AT: 4_000_000_000,  # far future: ensure_valid() will NOT refresh on its own
    const.CONF_USER_ID: "user-1",
}

SOFT_401 = {"Body": '{ "error": "invalid_token", "message": "Access token expired or invalid" }\n', "status": 401}


def run(entry_data, script):
    FakeSession.calls = []
    FakeSession.script = {k: list(v) for k, v in script.items()}
    entry = FakeEntry(entry_data)
    hass = FakeHass(entry)
    a = auth.SfpAuth(hass, entry)
    result = asyncio.run(a.ensure_core_token_valid())
    return result, entry, hass, FakeSession.calls


# --- 1. soft 401 -> refresh -> retry ------------------------------------------
tok, entry, hass, calls = run(BASE, {
    "issue_core_token_ha": [(200, SOFT_401), (200, {"response": {"core_token": "core-new", "core_token_exp": 1_900_000_000}})],
    "ha_refresh_token": [(200, {"response": {"access_token": "at-new", "refresh_token": "rt-new", "expires_at": 1_900_000_000}})],
})
check("soft 401 while issuing core_token refreshes the access token and retries once",
      tok == "core-new" and [c[0] for c in calls] == ["issue_core_token_ha", "ha_refresh_token", "issue_core_token_ha"],
      f"tok={tok} calls={[c[0] for c in calls]}")
check("the retry uses the NEW access token",
      calls[2][1].get("Authorization") == "Bearer at-new")
check("new tokens are stored on the entry",
      entry.data[const.CONF_ACCESS_TOKEN] == "at-new" and entry.data[const.CONF_CORE_TOKEN] == "core-new"
      and entry.data[const.CONF_CORE_TOKEN_EXP] == 1_900_000_000)

# --- 2. still rejected after refresh: give up, no loop ------------------------
tok, entry, hass, calls = run(BASE, {
    "issue_core_token_ha": [(200, SOFT_401), (200, SOFT_401)],
    "ha_refresh_token": [(200, {"response": {"access_token": "at-new", "refresh_token": "rt-new", "expires_at": 1_900_000_000}})],
})
check("a second soft 401 after refreshing gives up instead of looping",
      tok is None and len(calls) == 3)

# --- 3. refresh itself fails: give up ----------------------------------------
tok, entry, hass, calls = run(BASE, {
    "issue_core_token_ha": [(200, SOFT_401)],
    "ha_refresh_token": [(200, SOFT_401)],
})
check("a failed refresh after a soft 401 gives up and keeps the old tokens",
      tok is None and entry.data[const.CONF_ACCESS_TOKEN] == "at-old" and len(calls) == 2)

# --- 4. workflow returned {}: not an auth problem, no refresh -----------------
tok, entry, hass, calls = run(BASE, {"issue_core_token_ha": [(200, {})]})
check("an empty workflow response fails without touching the access token",
      tok is None and [c[0] for c in calls] == ["issue_core_token_ha"])

# --- 5. HTTP 401 (not soft) is treated the same as a soft 401 ----------------
tok, entry, hass, calls = run(BASE, {
    "issue_core_token_ha": [(401, "unauthorized"), (200, {"response": {"core_token": "core-new"}})],
    "ha_refresh_token": [(200, {"response": {"access_token": "at-new", "expires_at": 1_900_000_000}})],
})
check("a real HTTP 401 also refreshes and retries", tok == "core-new" and len(calls) == 3)

# --- 6. valid cached core token is reused without any call ------------------
tok, entry, hass, calls = run({**BASE, const.CONF_CORE_TOKEN: "core-cached", const.CONF_CORE_TOKEN_EXP: 4_000_000_000}, {})
check("a valid cached core token is returned with no network call", tok == "core-cached" and calls == [])

# --- 7. millisecond epochs ----------------------------------------------------
check("epoch milliseconds are normalized to seconds",
      auth.normalize_epoch_seconds(1_759_000_000_000) == 1_759_000_000
      and auth.normalize_epoch_seconds("1759000000") == 1_759_000_000
      and auth.normalize_epoch_seconds(None) is None and auth.normalize_epoch_seconds("x") is None)
tok, entry, hass, calls = run({**BASE, const.CONF_CORE_TOKEN: "core-stale", const.CONF_CORE_TOKEN_EXP: 1_600_000_000_000}, {
    "issue_core_token_ha": [(200, {"response": {"core_token": "core-new", "exp": 1_900_000_000_000}})],
})
check("a core token whose expiry was stored in milliseconds (already past) is re-issued, and the new ms expiry is stored in seconds",
      tok == "core-new" and entry.data[const.CONF_CORE_TOKEN_EXP] == 1_900_000_000)

# --- 8. soft-401 detector on the exact body seen in the log ------------------
check("is_bubble_soft_401 recognises the logged Bubble body",
      auth.is_bubble_soft_401(json.dumps(SOFT_401)) and not auth.is_bubble_soft_401("{}")
      and not auth.is_bubble_soft_401(json.dumps({"response": {"core_token": "x"}})))

print("\nAll checks passed" if failures == 0 else f"\n{failures} check(s) failed")
sys.exit(0 if failures == 0 else 1)
