from __future__ import annotations
import asyncio, logging, aiohttp
from datetime import timedelta
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class SmartFilterProCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.session = aiohttp.ClientSession()
        self.api_base = entry.data["api_base"]
        self.token = entry.data.get("token")  # however you store auth
        update_interval = timedelta(minutes=5)
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self._state = {}  # latest device data

    async def _async_update_data(self):
        # Poll fallback (if webhook not firing), fetch current state
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            async with self.session.get(f"{self.api_base}", headers=headers) as r:
                r.raise_for_status()
                data = await r.json()
                self._state = data  # normalize to {device_id: {...}}
                return self._state
        except Exception as e:
            _LOGGER.exception("SmartFilterPro poll failed: %s", e)
            raise

    @callback
    async def async_handle_webhook(self, hass, webhook_id, request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        # Example payload normalization
        # payload = {"device_id":"...","filter_life_pct":88,"runtime_hours":312,"next_change":"2025-09-20","hvac_status":"cooling"}
        dev_id = payload.get("device_id")
        if dev_id:
            self._state.setdefault(dev_id, {}).update(payload)
            self.async_set_updated_data(self._state)
        return "ok"
