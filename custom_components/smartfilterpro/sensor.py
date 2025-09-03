from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Optional, Dict

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
    CONF_REFRESH_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_PATH,
    CONF_API_BASE,
    CONF_HVAC_UID,
    DEFAULT_REFRESH_PATH,
    # skew (seconds) before expiry to refresh early
    TOKEN_SKEW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

K_PERCENT = "percentage_used"
K_TODAY   = "today_minutes"
K_TOTAL   = "total_minutes"

FALLBACK_KEYS = {
    K_PERCENT: ("percentage", "percent_used", "percentage used"),
    K_TODAY:   ("today", "todays_minutes", "2.0.1_Daily Active Time Sum"),
    K_TOTAL:   ("total", "total_runtime", "1.0.1_Minutes active"),
}


class SfpStatusCoordinator(DataUpdateCoordinator[dict]):
    """Poll the Bubble status workflow via POST + Bearer, with auto refresh."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self._status_url: str = entry.data.get(CONF_STATUS_URL) or ""
        self._api_base: str = (entry.data.get(CONF_API_BASE) or "").rstrip("/")
        self._refresh_path: str = (entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")

        if not self._status_url:
            raise ValueError("SmartFilterPro: missing status_url in config entry")

        self._session: aiohttp.ClientSession = async_get_clientsession(hass)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(minutes=20),
        )

    # -------- token helpers (local; no extra file needed) --------
    def _access_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_ACCESS_TOKEN)

    def _refresh_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_REFRESH_TOKEN)

    def _expires_at(self) -> Optional[int]:
        v = self.entry.data.get(CONF_EXPIRES_AT)
        return int(v) if v is not None else None

    async def _ensure_valid_token(self) -> None:
        exp = self._expires_at()
        if exp is None:
            return  # treat as long-lived
        now = int(time.time())
        if now < exp - TOKEN_SKEW_SECONDS:
            return
        await self._refresh_access_token()

    async def _refresh_access_token(self) -> None:
        rt = self._refresh_token()
        if not rt:
            _LOGGER.warning("No refresh_token; cannot refresh access token.")
            return

        url = f"{self._api_base}/{self._refresh_path}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"refresh_token": rt}, timeout=20) as r:
                    text = await r.text()
                    if r.status >= 400:
                        _LOGGER.error("Refresh %s -> %s %s", url, r.status, text[:500])
                        return
                    data = json.loads(text)
        except Exception as e:
            _LOGGER.error("Refresh call failed: %s", e)
            return

        body = data.get("response", data) if isinstance(data, dict) else {}
        at = body.get("access_token")
        new_rt = body.get("refresh_token", rt)
        exp = body.get("expires_at")

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
        # refresh our local handle to the entry
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        _LOGGER.debug("Token refreshed; exp=%s", exp)

    # ------------------ main poll ------------------
    async def _async_update_data(self) -> dict:
        await self._ensure_valid_token()
        token = self._access_token()
        hvac_uid = self.entry.data.get(CONF_HVAC_UID)

        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload: Dict[str, str] = {}
        if hvac_uid:
            payload["hvac_uid"] = hvac_uid

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
        # already logged; entities will become available on next success
        pass

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
