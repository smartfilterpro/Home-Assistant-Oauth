from __future__ import annotations

import aiohttp, asyncio, json, logging, time
from typing import Optional

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    CONF_API_BASE,
    CONF_RESET_PATH,
    CONF_USER_ID,
    CONF_HVAC_ID,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_PATH,
    DEFAULT_RESET_PATH,
    DEFAULT_REFRESH_PATH,
    TOKEN_SKEW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


async def _ensure_valid_token(hass: HomeAssistant, entry: ConfigEntry) -> Optional[str]:
    """Refresh access token if nearing expiry; return current token."""
    def _exp() -> Optional[int]:
        v = entry.data.get(CONF_EXPIRES_AT)
        return int(v) if v is not None else None

    exp = _exp()
    if exp is None:
        return entry.data.get(CONF_ACCESS_TOKEN)

    now = int(time.time())
    if now < exp - TOKEN_SKEW_SECONDS:
        return entry.data.get(CONF_ACCESS_TOKEN)

    rt = entry.data.get(CONF_REFRESH_TOKEN)
    if not rt:
        _LOGGER.warning("No refresh_token; cannot refresh before reset call.")
        return entry.data.get(CONF_ACCESS_TOKEN)

    api_base = (entry.data.get(CONF_API_BASE) or "").rstrip("/")
    refresh_path = (entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
    url = f"{api_base}/{refresh_path}"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"refresh_token": rt}, timeout=20) as r:
                text = await r.text()
                if r.status >= 400:
                    _LOGGER.error("Refresh %s -> %s %s", url, r.status, text[:500])
                    return entry.data.get(CONF_ACCESS_TOKEN)
                data = json.loads(text)
    except Exception as e:
        _LOGGER.error("Refresh call failed: %s", e)
        return entry.data.get(CONF_ACCESS_TOKEN)

    body = data.get("response", data) if isinstance(data, dict) else {}
    at = body.get("access_token")
    new_rt = body.get("refresh_token", rt)
    new_exp = body.get("expires_at")
    if not at or new_exp is None:
        _LOGGER.error("Refresh response missing access_token/expires_at: %s", body)
        return entry.data.get(CONF_ACCESS_TOKEN)

    new_data = dict(entry.data)
    new_data.update({
        CONF_ACCESS_TOKEN: at,
        CONF_REFRESH_TOKEN: new_rt,
        CONF_EXPIRES_AT: int(new_exp),
    })
    hass.config_entries.async_update_entry(entry, data=new_data)
    return at


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    async_add_entities([SmartFilterProResetButton(hass, entry)], True)


class SmartFilterProResetButton(ButtonEntity):
    _attr_name = "Reset Filter Usage"
    _attr_icon = "mdi:filter-reset"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        hvac_id = entry.data.get(CONF_HVAC_ID, "unknown")
        self._attr_unique_id = f"{DOMAIN}_reset_{hvac_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hvac_id)},
            name=f"SmartFilterPro ({hvac_id})",
            manufacturer="SmartFilterPro",
            model="Filter telemetry bridge",
        )

    async def async_press(self) -> None:
        api_base = (self.entry.data.get(CONF_API_BASE) or "").rstrip("/")
        reset_path = (self.entry.data.get(CONF_RESET_PATH) or DEFAULT_RESET_PATH).strip("/")
        user_id = self.entry.data.get(CONF_USER_ID)
        hvac_id = self.entry.data.get(CONF_HVAC_ID)

        if not api_base or not user_id or not hvac_id:
            _LOGGER.error("Reset aborted: missing api_base/user_id/hvac_id")
            return

        # Ensure token is fresh
        token = await _ensure_valid_token(self.hass, self.entry)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url = f"{api_base}/{reset_path}"
        ok = False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    json={"user_id": user_id, "hvac_id": hvac_id},
                    headers=headers,
                    timeout=25,
                ) as resp:
                    txt = await resp.text()
                    if resp.status >= 400:
                        _LOGGER.error("Reset POST %s -> %s %s", url, resp.status, txt[:500])
                    else:
                        _LOGGER.debug("Reset OK: %s", txt[:250])
                        ok = True
        except Exception as e:
            _LOGGER.error("Reset request failed: %s", e)

        # Kick the status coordinator to re-poll now + shortly after
        if ok:
            coord = (self.hass.data.get(DOMAIN, {})
                                .get(self.entry.entry_id, {})
                                .get("status_coord"))
            if coord:
                await coord.async_request_refresh()
                async def _delayed():
                    try:
                        await asyncio.sleep(3)
                        await coord.async_request_refresh()
                    except Exception:
                        pass
                asyncio.create_task(_delayed())
