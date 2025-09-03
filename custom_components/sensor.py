from __future__ import annotations
import aiohttp
import logging
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from .const import DOMAIN, CONF_DATA_OBJ_URL

_LOGGER = logging.getLogger(__name__)

# Bubble field keys exactly as in your Data API object
FIELD_PERCENT = "percentage used"                  # As Percentage of Filter Used
FIELD_TODAY   = "2.0.1_Daily Active Time Sum"     # today's usage (minutes)
FIELD_TOTAL   = "1.0.1_Minutes active"            # total time used on filter (minutes)


class SfpObjCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.url = entry.data[CONF_DATA_OBJ_URL]   # full /obj/thermostats/<id>
        self.session = aiohttp.ClientSession()
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_obj", update_interval=timedelta(minutes=20))

    async def _async_update_data(self):
        try:
            async with self.session.get(self.url, timeout=20) as resp:
                resp.raise_for_status()
                data = await resp.json()
                # Bubble Data API usually: {"response": { <fields>... }}
                body = data.get("response") or data
                return body
        except Exception as e:
            _LOGGER.error("SmartFilterPro Data API fetch failed: %s", e)
            raise


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coord = SfpObjCoordinator(hass, entry)
    await coord.async_config_entry_first_refresh()

    sensors = [
        SfpFieldSensor(coord, FIELD_PERCENT, "SmartFilterPro Percentage Used", "%", round_1=True),
        SfpFieldSensor(coord, FIELD_TODAY,   "SmartFilterPro Today's Usage",   "min"),
        SfpFieldSensor(coord, FIELD_TOTAL,   "SmartFilterPro Total Minutes",   "min"),
    ]
    async_add_entities(sensors)


class SfpFieldSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: SfpObjCoordinator, field_key: str, name: str, unit: str | None, round_1: bool=False):
        super().__init__(coordinator)
        self._key = field_key
        self._attr_name = name
        # unique_id can't contain spaces well; hash key
        uid_key = field_key.replace(" ", "_").replace(".", "_")
        self._attr_unique_id = f"{DOMAIN}_{uid_key}"
        self._attr_native_unit_of_measurement = unit
        self._round_1 = round_1

    @property
    def native_value(self):
        body = self.coordinator.data or {}
        val = body.get(self._key)
        if self._round_1 and isinstance(val, (int, float)):
            return round(float(val), 1)
        return val
