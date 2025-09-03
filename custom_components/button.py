from __future__ import annotations
import aiohttp, logging
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from .const import (
    DOMAIN, CONF_API_BASE, CONF_RESET_PATH, CONF_USER_ID, CONF_HVAC_ID,
    DEFAULT_RESET_PATH,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    async_add_entities([SmartFilterProResetButton(hass, entry)], True)

class SmartFilterProResetButton(ButtonEntity):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self._attr_name = "Reset Filter Usage"
        self._attr_unique_id = f"{DOMAIN}_reset_{entry.data.get(CONF_HVAC_ID, 'unknown')}"
        self._attr_icon = "mdi:filter-reset"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data.get(CONF_HVAC_ID, "unknown"))},
            name=f"SmartFilterPro ({entry.data.get(CONF_HVAC_ID, '')})",
            manufacturer="SmartFilterPro",
            model="Filter telemetry bridge",
        )

    async def async_press(self) -> None:
        api_base = self.entry.data.get(CONF_API_BASE, "").rstrip("/")
        reset_path = self.entry.data.get(CONF_RESET_PATH, DEFAULT_RESET_PATH).strip("/")
        user_id = self.entry.data.get(CONF_USER_ID)
        hvac_id = self.entry.data.get(CONF_HVAC_ID)

        if not api_base or not user_id or not hvac_id:
            _LOGGER.error("Reset aborted: missing api_base/user_id/hvac_id in entry data")
            return

        url = f"{api_base}/{reset_path}"
        payload = {"user_id": user_id, "hvac_id": hvac_id}

        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=20) as resp:
                    txt = await resp.text()
                    if resp.status >= 400:
                        _LOGGER.error("Reset POST %s -> %s %s | payload=%s", url, resp.status, txt[:500], payload)
                    else:
                        _LOGGER.debug("Reset OK: %s", txt[:200])
        except Exception as e:
            _LOGGER.error("Reset request failed: %s", e)
