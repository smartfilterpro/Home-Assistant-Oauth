from __future__ import annotations
import time, json, logging, aiohttp
from typing import Optional
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from .const import (
    CONF_API_BASE, CONF_REFRESH_PATH, CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN, CONF_EXPIRES_AT, DEFAULT_REFRESH_PATH, TOKEN_SKEW_SECONDS,
    CONF_CORE_JWT_PATH, CONF_CORE_TOKEN, CONF_CORE_TOKEN_EXP,
    CONF_USER_ID, DEFAULT_CORE_JWT_PATH, CORE_TOKEN_SKEW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


def normalize_epoch_seconds(value) -> Optional[int]:
    """Coerce an epoch value to whole seconds.

    Bubble's "extract UNIX" gives milliseconds; our own math gives seconds.
    A millisecond value stored as seconds puts expiry ~50,000 years out, so
    the access token is never refreshed and every Bubble call fails with a
    soft 401 forever. Anything above 1e11 (year 5138 in seconds) is treated
    as milliseconds.
    """
    if value is None or value == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    if n > 100_000_000_000:
        n //= 1000
    return n


def is_bubble_soft_401(txt: str) -> bool:
    """
    Detect Bubble's 'HTTP 200 but auth failed' pattern, where the JSON body
    encodes status=401 or error='invalid_token'.
    """
    try:
        data = json.loads(txt) if txt else {}
    except Exception:
        return False

    body = data.get("response", data) if isinstance(data, dict) else {}

    def _has_invalid(x) -> bool:
        if not isinstance(x, dict):
            # Check if x is a JSON string that contains error info
            if isinstance(x, str):
                xl = x.lower()
                return "invalid_token" in xl or ("access token" in xl and ("invalid" in xl or "expired" in xl))
            return False
        status = x.get("status") or x.get("status_code")
        if isinstance(status, str):
            try:
                status = int(status)
            except Exception:
                pass
        if status == 401:
            return True
        err = str(x.get("error", "")).lower()
        msg = str(x.get("message", "")).lower()
        return ("invalid_token" in err) or ("access token" in msg and "invalid" in msg)

    if _has_invalid(body):
        return True
    # Check nested Body/body field (Bubble uses capital B)
    if isinstance(body, dict):
        nested = body.get("Body") or body.get("body")
        if _has_invalid(nested):
            return True
    return False


class SfpAuth:
    """Centralized token helper for SmartFilterPro."""
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry

    @property
    def access_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_ACCESS_TOKEN)

    @property
    def refresh_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_REFRESH_TOKEN)

    @property
    def expires_at(self) -> Optional[int]:
        return normalize_epoch_seconds(self.entry.data.get(CONF_EXPIRES_AT))

    async def ensure_valid(self) -> None:
        exp = self.expires_at
        if exp is None:
            return  # treat as long-lived
        if int(time.time()) < exp - TOKEN_SKEW_SECONDS:
            return
        await self._refresh()

    async def force_refresh(self) -> bool:
        """Refresh the Bubble access token now, whatever expires_at says.

        Used when Bubble rejects the stored token before its recorded
        expiry (a soft 401). Writes the entry once, unlike the old trick of
        back-dating expires_at and then calling ensure_valid().
        """
        return await self._refresh()

    async def _refresh(self) -> bool:
        """Refresh tokens. Returns True on success, False on failure."""
        rt = self.refresh_token
        if not rt:
            _LOGGER.warning("No refresh_token; cannot refresh. Please delete and re-add the integration.")
            return False
        base = (self.entry.data.get(CONF_API_BASE) or "").rstrip("/")
        path = (self.entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
        url  = f"{base}/{path}"

        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"refresh_token": rt}, timeout=20) as r:
                    txt = await r.text()
                    if r.status >= 400 or is_bubble_soft_401(txt):
                        _LOGGER.error(
                            "Token refresh failed (%s). Your session may have expired. "
                            "Please delete and re-add the SmartFilterPro integration. Response: %s",
                            r.status, txt[:400]
                        )
                        return False
                    data = json.loads(txt) if txt else {}
        except Exception as e:
            _LOGGER.error("Refresh call failed: %s", e)
            return False

        body = data.get("response", data) if isinstance(data, dict) else {}
        at  = body.get("access_token")
        exp = normalize_epoch_seconds(body.get("expires_at"))
        if exp is None and body.get("expires_in") is not None:
            exp = normalize_epoch_seconds(int(time.time()) + int(float(body["expires_in"])))
        new_rt = body.get("refresh_token", rt)

        if not at or exp is None:
            _LOGGER.error(
                "Refresh response missing access_token/expires_at. "
                "Please delete and re-add the SmartFilterPro integration. Response: %s", body
            )
            return False

        new_data = dict(self.entry.data)
        new_data.update({
            CONF_ACCESS_TOKEN: at,
            CONF_REFRESH_TOKEN: new_rt,
            CONF_EXPIRES_AT: int(exp),
        })
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        # re-fetch entry so future reads see updated tokens
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        _LOGGER.debug("Token refreshed; exp=%s", exp)
        return True

    # ========== Core Token (for Railway Core Ingest) ==========

    @property
    def core_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_CORE_TOKEN)

    @property
    def core_token_exp(self) -> Optional[int]:
        return normalize_epoch_seconds(self.entry.data.get(CONF_CORE_TOKEN_EXP))

    async def ensure_core_token_valid(self) -> Optional[str]:
        """Ensure Core token is valid; refresh if expired. Returns token or None."""
        exp = self.core_token_exp
        now_sec = int(time.time())

        if self.core_token and exp and now_sec < (exp - CORE_TOKEN_SKEW_SECONDS):
            _LOGGER.debug("Core token valid (expires in %ss)", exp - now_sec)
            return self.core_token

        _LOGGER.debug("Core token expired or missing, requesting new one...")
        return await self._issue_core_token()

    async def _issue_core_token(self, _retry: bool = False) -> Optional[str]:
        """Request new Core JWT from Bubble's HA-specific endpoint.

        Bubble answers an expired or revoked access token with HTTP 200 and a
        body carrying status 401 (a "soft 401"). That used to be logged as
        "Core token response missing token" and given up on — 125 times in
        one evening on a live hub — without ever refreshing the access token
        that caused it. Now a rejected access token is refreshed once and the
        request retried, the same recovery the Hubitat app has.
        """
        # First ensure we have a valid Bubble access token
        await self.ensure_valid()

        at = self.access_token
        if not at:
            _LOGGER.warning("Cannot issue core token — no Bubble access_token")
            return None

        base = (self.entry.data.get(CONF_API_BASE) or "").rstrip("/")
        path = (self.entry.data.get(CONF_CORE_JWT_PATH) or DEFAULT_CORE_JWT_PATH).strip("/")
        url = f"{base}/{path}"
        user_id = self.entry.data.get(CONF_USER_ID)

        try:
            _LOGGER.info("Requesting new core_token from Bubble: %s", url)
            async with aiohttp.ClientSession() as s:
                headers = {"Authorization": f"Bearer {at}"}
                async with s.post(url, json={"user_id": user_id}, headers=headers, timeout=20) as r:
                    txt = await r.text()
                    status = r.status
        except Exception as e:
            _LOGGER.error("Core token request exception: %s", e)
            return None

        if status == 401 or is_bubble_soft_401(txt):
            if not _retry:
                _LOGGER.info("Bubble rejected the access token while issuing core_token; refreshing it and retrying once")
                if await self._refresh():
                    return await self._issue_core_token(_retry=True)
                _LOGGER.error("Core token request rejected (401) and the access-token refresh failed; re-add the integration if this persists")
                return None
            _LOGGER.error("Core token request still rejected (401) after refreshing the access token")
            return None

        if status >= 400:
            _LOGGER.error("Core token request failed: %s -> %s %s", url, status, txt[:400])
            return None

        try:
            data = json.loads(txt) if txt else {}
        except ValueError:
            _LOGGER.error("Core token response is not JSON (HTTP %s): %s", status, txt[:200])
            return None

        body = data.get("response", data) if isinstance(data, dict) else {}

        # Extract token (Bubble may use different field names)
        core = body.get("core_token") or body.get("token") or ""
        exp = normalize_epoch_seconds(body.get("core_token_exp") or body.get("exp") or body.get("expires_at"))

        if not core:
            # Bubble ran the workflow but returned no token. Not an auth
            # failure — the workflow's "Return data" step did not fire,
            # usually a condition on it (e.g. one that only passes on live,
            # not version-test). Nothing here can fix that; say so plainly.
            _LOGGER.error(
                "Bubble's issue_core_token_ha workflow returned no core_token (keys: %s). "
                "Check that workflow's Return data step and its conditions in this environment (%s).",
                sorted(body.keys()) if isinstance(body, dict) else type(body).__name__, base,
            )
            return None

        # Store the new Core token
        new_data = dict(self.entry.data)
        new_data[CONF_CORE_TOKEN] = core
        if exp:
            new_data[CONF_CORE_TOKEN_EXP] = int(exp)

        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        _LOGGER.info("Core token refreshed (exp: %s)", exp)
        return core
