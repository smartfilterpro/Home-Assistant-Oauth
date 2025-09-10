from __future__ import annotations
import time, json, logging, aiohttp
from typing import Optional
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from .const import (
    CONF_API_BASE, CONF_REFRESH_PATH, CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN, CONF_EXPIRES_AT, DEFAULT_REFRESH_PATH, TOKEN_SKEW_SECONDS
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
