from __future__ import annotations
import time, json, logging, aiohttp
from typing import Optional
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from .const import (CONF_API_BASE, CONF_REFRESH_PATH, CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN, CONF_EXPIRES_AT, DEFAULT_REFRESH_PATH, TOKEN_SKEW_SECONDS)

_LOGGER = logging.getLogger(__name__)

class SfpAuth:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry

    @property
    def access_token(self) -> Optional[str]: return self.entry.data.get(CONF_ACCESS_TOKEN)
    @property
    def refresh_token(self) -> Optional[str]: return self.entry.data.get(CONF_REFRESH_TOKEN)
    @property
    def expires_at(self) -> Optional[int]:
        v = self.entry.data.get(CONF_EXPIRES_AT); return int(v) if v is not None else None

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
            _LOGGER.warning("No refresh_token; cannot refresh."); return
        base = (self.entry.data.get(CONF_API_BASE) or "").rstrip("/")
        path = (self.entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
        url  = f"{base}/{path}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"refresh_token": rt}, timeout=20) as r:
                    txt = await r.text()
                    if r.status >= 400:
                        _LOGGER.error("Refresh %s -> %s %s", url, r.status, txt[:400]); return
                    data = json.loads(txt)
        except Exception as e:
            _LOGGER.error("Refresh call failed: %s", e); return
        body = data.get("response", data) if isinstance(data, dict) else {}
        at = body.get("access_token"); exp = body.get("expires_at"); new_rt = body.get("refresh_token", rt)
        if not at or exp is None:
            _LOGGER.error("Refresh response missing access_token/expires_at: %s", body); return
        new_data = dict(self.entry.data)
        new_data.update({CONF_ACCESS_TOKEN: at, CONF_REFRESH_TOKEN: new_rt, CONF_EXPIRES_AT: int(exp)})
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        _LOGGER.debug("Token refreshed; exp=%s", exp)
