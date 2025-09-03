from __future__ import annotations

import aiohttp, asyncio, logging
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
    DEFAULT_RESET_PATH,
)

_LOGGER = logging.getLogger(__name__)

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
        access_token = self.entry.data.get(CONF_ACCESS_TOKEN)

        if not api_base or not user_id or not hvac_id:
            _LOGGER.error("Reset aborted: missing api_base/user_id/hvac_id")
            return

        url = f"{api_base}/{reset_path}"
        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        ok = False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"user_id": user_id, "hvac_id": hvac_id}, headers=headers, timeout=25) as resp:
                    txt = await resp.text()
                    if resp.status >= 400:
                        _LOGGER.error("Reset POST %s -> %s %s", url, resp.status, txt[:500])
                    else:
                        _LOGGER.debug("Reset OK: %s", txt[:250])
                        ok = True
        except Exception as e:
            _LOGGER.error("Reset request failed: %s", e)

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