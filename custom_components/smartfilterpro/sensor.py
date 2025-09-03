from __future__ import annotations

import logging
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

from .const import (
    DOMAIN,
    CONF_STATUS_URL,
    CONF_ACCESS_TOKEN,
    CONF_HVAC_UID,
)

_LOGGER = logging.getLogger(__name__)

# Expected payload from your workflow (either raw or under "response")
# {
#   "percentage_used": <number>,
#   "today_minutes":   <number>,
#   "total_minutes":   <number>
# }
K_PERCENT = "percentage_used"
K_TODAY   = "today_minutes"
K_TOTAL   = "total_minutes"

FALLBACK_KEYS = {
    K_PERCENT: ("percentage", "percent_used", "percentage used"),
    K_TODAY:   ("today", "todays_minutes", "2.0.1_Daily Active Time Sum"),
    K_TOTAL:   ("total", "total_runtime", "1.0.1_Minutes active"),
}

class SfpStatusCoordinator(DataUpdateCoordinator[dict]):
    """Poll the Bubble status workflow via POST + Bearer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self._status_url: str = entry.data.get(CONF_STATUS_URL) or ""
        self._token: str | None = entry.data.get(CONF_ACCESS_TOKEN)
        self._hvac_uid: str | None = entry.data.get(CONF_HVAC_UID)

        if not self._status_url:
            raise ValueError("SmartFilterPro: missing status_url in config entry")

        self._session: aiohttp.ClientSession = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(minutes=20),
        )

    async def _async_update_data(self) -> dict:
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        payload = {}
        if self._hvac_uid:
            payload["hvac_uid"] = self._hvac_uid   # never in URL; body only

        try:
            async with self._session.post(
                self._status_url, json=(payload or None), headers=headers, timeout=25
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"Status POST {self._status_url} -> {resp.status} {text[:500]}"
                    )
                try:
                    data = await resp.json()
                except Exception as e:
                    raise RuntimeError(f"Non-JSON response: {text[:300]}") from e
        except Exception as e:
            _LOGGER.error("SmartFilterPro status fetch failed: %s", e)
            raise

        body = data.get("response") if isinstance(data, dict) else None
        if not body:
            body = data
        if not isinstance(body, dict):
            raise RuntimeError(f"Unexpected JSON shape: {body!r}")

        def pick(key, *alts):
            if key in body:
                return body[key]
            for a in alts:
                if a in body:
                    return body[a]
            return None

        return {
            K_PERCENT: pick(K_PERCENT, *FALLBACK_KEYS[K_PERCENT]),
            K_TODAY:   pick(K_TODAY,   *FALLBACK_KEYS[K_TODAY]),
            K_TOTAL:   pick(K_TOTAL,   *FALLBACK_KEYS[K_TOTAL]),
        }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coord = SfpStatusCoordinator(hass, entry)
    try:
        await coord.async_config_entry_first_refresh()
    except Exception:
        # Already logged; entities will show unavailable until next success
        pass

    # Allow the Reset button to refresh after POST
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["status_coord"] = coord

    entities = [
        SfpFieldSensor(coord, K_PERCENT, "SmartFilterPro Percentage Used", "%", round_1=True, uid="percentage_used"),
        SfpFieldSensor(coord, K_TODAY,   "SmartFilterPro Today's Usage",   "min", uid="todays_usage"),
        SfpFieldSensor(coord, K_TOTAL,   "SmartFilterPro Total Minutes",   "min", uid="total_minutes"),
    ]
    async_add_entities(entities)


class SfpFieldSensor(CoordinatorEntity[SfpStatusCoordinator], SensorEntity):
    def __init__(self, coordinator, field_key, name, unit, *, round_1=False, uid: str):
        super().__init__(coordinator)
        self._key = field_key
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{uid}"
        self._attr_native_unit_of_measurement = unit
        self._round_1 = round_1

    @property
    def native_value(self):
        val = (self.coordinator.data or {}).get(self._key)
        if self._round_1 and isinstance(val, (int, float)):
            try:
                return round(float(val), 1)
            except Exception:
                return val
        return val