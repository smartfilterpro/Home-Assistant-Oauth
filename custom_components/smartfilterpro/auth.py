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
    if isinstance(body, dict) and _has_invalid(body.get("body", {})):
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
        v = self.entry.data.get(CONF_EXPIRES_AT)
        return int(v) if v is not None else None

    async def ensure_valid(self) -> None:
        exp = self.expires_at
        if exp is None:
            return  # treat as long-lived
        if int(time.time()) < exp - TOKEN_SKEW_SECONDS:
            return
        await self._refresh()

    async def _refresh(self) -> None:
        rt = self.refresh_token
        if not rt:
            _LOGGER.warning("No refresh_token; cannot refresh.")
            return
        base = (self.entry.data.get(CONF_API_BASE) or "").rstrip("/")
        path = (self.entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
        url  = f"{base}/{path}"

        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"refresh_token": rt}, timeout=20) as r:
                    txt = await r.text()
                    if r.status >= 400:
                        _LOGGER.error("Refresh %s -> %s %s", url, r.status, txt[:400])
                        return
                    data = json.loads(txt) if txt else {}
        except Exception as e:
            _LOGGER.error("Refresh call failed: %s", e)
            return

        body = data.get("response", data) if isinstance(data, dict) else {}
        at  = body.get("access_token")
        exp = body.get("expires_at")
        new_rt = body.get("refresh_token", rt)

        if not at or exp is None:
            _LOGGER.error("Refresh response missing access_token/expires_at: %s", body)
            return

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

    # ========== Core Token (for Railway Core Ingest) ==========

    @property
    def core_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_CORE_TOKEN)

    @property
    def core_token_exp(self) -> Optional[int]:
        v = self.entry.data.get(CONF_CORE_TOKEN_EXP)
        return int(v) if v is not None else None

    async def ensure_core_token_valid(self) -> Optional[str]:
        """Ensure Core token is valid; refresh if expired. Returns token or None."""
        exp = self.core_token_exp
        now_sec = int(time.time())

        if self.core_token and exp and now_sec < (exp - CORE_TOKEN_SKEW_SECONDS):
            _LOGGER.debug("Core token valid (expires in %ss)", exp - now_sec)
            return self.core_token

        _LOGGER.debug("Core token expired or missing, requesting new one...")
        return await self._issue_core_token()

    async def _issue_core_token(self) -> Optional[str]:
        """Request new Core JWT from Bubble's HA-specific endpoint."""
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
                    if r.status >= 400:
                        _LOGGER.error("Core token request failed: %s -> %s %s", url, r.status, txt[:400])
                        return None
                    data = json.loads(txt) if txt else {}
        except Exception as e:
            _LOGGER.error("Core token request exception: %s", e)
            return None

        body = data.get("response", data) if isinstance(data, dict) else {}

        # Extract token (Bubble may use different field names)
        core = body.get("core_token") or body.get("token") or ""
        exp = body.get("core_token_exp") or body.get("exp") or body.get("expires_at")

        if not core:
            _LOGGER.error("Core token response missing token: %s", body)
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
