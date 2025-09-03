# custom_components/smartfilterpro/sensor.py
from __future__ import annotations

import logging
import time
from datetime import timedelta

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN, CONF_DATA_OBJ_URL

_LOGGER = logging.getLogger(__name__)

# Keys expected on your Bubble Data API object
FIELD_PERCENT = "percentage used"
FIELD_TODAY = "2.0.1_Daily Active Time Sum"
FIELD_TOTAL = "1.0.1_Minutes active"


class SfpObjCoordinator(DataUpdateCoordinator[dict]):
    """Coordinator that polls the Bubble Data API object."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._base_url = entry.data.get(CONF_DATA_OBJ_URL, "")
        if not self._base_url:
            raise ValueError("Missing CONF_DATA_OBJ_URL in config entry data")

        self._access_token = entry.data.get("access_token")
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_obj",
            update_interval=timedelta(minutes=20),
        )

    def _cache_busted_url(self) -> str:
        ts = int(time.time() * 1000)
        sep = "&" if "?" in self._base_url else "?"
        return f"{self._base_url}{sep}_ts={ts}"

    async def _async_update_data(self) -> dict:
        url = self._cache_busted_url()
        headers = {"Cache-Control": "no-cache"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        try:
            async with self._session.get(url, headers=headers, timeout=20) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # Raise so HA shows it in logs and marks coordinator failed (not uninstalling the platform)
                    raise RuntimeError(
                        f"Data API GET {url} -> {resp.status} {text[:500]}"
                    )
                # The Bubble Data API sometimes returns {"response": {...}}
                try:
                    data = await resp.json()
                except Exception as e:
                    raise RuntimeError(f"Non-JSON response from Data API: {text[:300]}") from e

                body = data.get("response") or data
                if not isinstance(body, dict):
                    raise RuntimeError(f"Unexpected JSON shape: {body!r}")
                return body
        except Exception as e:
            _LOGGER.error("SmartFilterPro Data API fetch failed: %s", e)
            # Re-raise so HA shows the error and the entities become unavailable (not removed)
            raise


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up SmartFilterPro sensors."""
    try:
        coord = SfpObjCoordinator(hass, entry)
    except Exception as e:
        _LOGGER.exception("Sensor setup failed before first refresh: %s", e)
        return

    # First refresh; if this raises, entities won't be added (but you'll see the log)
    try:
        await coord.async_config_entry_first_refresh()
    except Exception:
        # Error already logged in coordinator; continue so entities register as unavailable
        pass

    # Stash the coordinator so the reset button can force refreshes
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["obj_coord"] = coord

    sensors = [
        SfpFieldSensor(coord, FIELD_PERCENT, "SmartFilterPro Percentage Used", "%", round_1=True, uid="percentage_used"),
        SfpFieldSensor(coord, FIELD_TODAY, "SmartFilterPro Today's Usage", "min", uid="todays_usage"),
        SfpFieldSensor(coord, FIELD_TOTAL, "SmartFilterPro Total Minutes", "min", uid="total_minutes"),
    ]
    async_add_entities(sensors)


class SfpFieldSensor(CoordinatorEntity[SfpObjCoordinator], SensorEntity):
    """Sensor that exposes a single field from the Bubble object."""

    def __init__(
        self,
        coordinator: SfpObjCoordinator,
        field_key: str,
        name: str,
        unit: str | None,
        *,
        round_1: bool = False,
        uid: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = field_key
        self._attr_name = name
        # Keep stable unique_ids so entities don’t “disappear” on updates
        self._attr_unique_id = f"{DOMAIN}_{uid}"
        self._attr_native_unit_of_measurement = unit
        self._round_1 = round_1

    @property
    def native_value(self):
        body = self.coordinator.data or {}
        val = body.get(self._key)
        if self._round_1 and isinstance(val, (int, float)):
            try:
                return round(float(val), 1)
            except Exception:  # defensive
                return val
        return val
