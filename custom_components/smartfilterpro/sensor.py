from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Optional, Dict, Any, Iterable

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    DOMAIN,
    CONF_STATUS_URL, CONF_POST_PATH,
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT,
    CONF_REFRESH_PATH, CONF_API_BASE,
    CONF_HVAC_ID, CONF_HVAC_UID, CONF_USER_ID,
    CONF_CLIMATE_ENTITY_ID,
    DEFAULT_REFRESH_PATH, TOKEN_SKEW_SECONDS,
)
from .auth import SfpAuth, is_bubble_soft_401

_LOGGER = logging.getLogger(__name__)

# Keys matching new Bubble response format
K_FILTER_HEALTH = "filter_health"
K_MINUTES_ACTIVE = "minutes_active"
K_LAST_UPDATED = "last_updated"

# Fallback keys to handle different Bubble response formats
FALLBACK_KEYS = {
    K_FILTER_HEALTH: ("filterHelath", "filterHealth", "filter_health", "percentage", "percent"),
    K_MINUTES_ACTIVE: ("minutesActive", "minutes_active", "total_runtime", "total_minutes", "1.0.1_Minutes active"),
    K_LAST_UPDATED: ("lastUpdated", "last_updated", "updated_at"),
}

# Fallback keys for device name
DEVICE_NAME_KEYS = ("deviceName", "device_name", "thermostat_name", "name")

def _pick(obj: Dict, *keys):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _normalize_hvac(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (list, tuple, set)):
        for item in val:
            return str(item)
        return None
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            js = s.replace("'", '"') if ("'" in s and '"' not in s) else s
            arr = json.loads(js)
            if isinstance(arr, Iterable):
                for item in arr:
                    return str(item)
        except Exception:
            s = s.strip("[]").strip().strip("'").strip('"')
            return s or None
    return s or None

def _combine_url(api_base: str, maybe_path: str) -> str:
    b = (api_base or "").rstrip("/")
    p = (maybe_path or "").strip()
    if not p:
        return b
    if p.lower().startswith(("http://", "https://")):
        return p
    return f"{b}/{p.lstrip('/')}"

class SfpStatusCoordinator(DataUpdateCoordinator[dict]):
    """Poll Bubble status; send Bearer; auto refresh; optional telemetry ping."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry

        self._status_url: str = entry.data.get(CONF_STATUS_URL) or ""
        self._api_base: str = (entry.data.get(CONF_API_BASE) or "").rstrip("/")
        self._refresh_path: str = (entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
        self._post_path: str = (entry.data.get(CONF_POST_PATH) or "").strip("/")

        if not self._status_url:
            raise ValueError("SmartFilterPro: missing status_url in config entry")

        self._status_full_url: str = _combine_url(self._api_base, self._status_url)
        _LOGGER.debug(
            "SmartFilterPro status base/path -> URL: base=%s path=%s url=%s",
            self._api_base, self._status_url, self._status_full_url
        )

        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_status", update_interval=timedelta(minutes=20))

    # --- token helpers via SfpAuth ---
    def _access_token(self) -> Optional[str]:
        tok = self.entry.data.get(CONF_ACCESS_TOKEN) or self.entry.data.get("token") or self.entry.data.get("id_token")
        return tok

    def _refresh_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_REFRESH_TOKEN)

    def _expires_at(self) -> Optional[int]:
        v = self.entry.data.get(CONF_EXPIRES_AT)
        return int(v) if v is not None else None

    async def _ensure_valid_token(self) -> None:
        auth = SfpAuth(self.hass, self.entry)
        await auth.ensure_valid()
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)

    async def _refresh_access_token(self) -> None:
        # Force refresh regardless of skew check
        auth = SfpAuth(self.hass, self.entry)
        # simulate forced refresh by temporarily setting expiry
        if auth.expires_at is not None:
            new_data = dict(self.entry.data)
            new_data[CONF_EXPIRES_AT] = int(time.time()) - 1
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        await auth.ensure_valid()
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)

    # --- main poll ---
    async def _async_update_data(self) -> dict:
        await self._ensure_valid_token()
        token = self._access_token()

        raw_hvac = self.entry.data.get(CONF_HVAC_UID) or self.entry.data.get(CONF_HVAC_ID)
        hvac_uid = _normalize_hvac(raw_hvac)

        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            _LOGGER.warning("No access token; sending status request without Authorization header.")

        payload: Dict[str, str] = {}
        if hvac_uid:
            payload["hvac_uid"] = hvac_uid

        url = self._status_full_url
        _LOGGER.debug("SmartFilterPro status fetch URL: %s", url)

        try:
            async with self._session.post(url, json=(payload or None), headers=headers, timeout=25) as resp:
                text = await resp.text()

                # Treat true 401s and Bubble soft-401s the same
                if resp.status == 401 or is_bubble_soft_401(text):
                    _LOGGER.warning(
                        "SmartFilterPro status unauthorized (HTTP=%s, soft401=%s). Refreshing & retrying once.",
                        resp.status, is_bubble_soft_401(text),
                    )
                    await self._refresh_access_token()
                    token2 = self._access_token()
                    headers2 = {"Accept": "application/json", "Cache-Control": "no-cache"}
                    if token2:
                        headers2["Authorization"] = f"Bearer {token2}"
                    async with self._session.post(url, json=(payload or None), headers=headers2, timeout=25) as r2:
                        t2 = await r2.text()
                        if r2.status >= 400 or is_bubble_soft_401(t2):
                            raise RuntimeError(f"Status retry POST {url} -> {r2.status} {t2[:500]}")
                        data = await r2.json()
                        body = data.get("response") if isinstance(data, dict) else data
                        if not isinstance(body, dict):
                            raise RuntimeError(f"Unexpected JSON shape: {body!r}")
                        filter_health = _pick(body, *FALLBACK_KEYS[K_FILTER_HEALTH])
                        minutes_active = _pick(body, *FALLBACK_KEYS[K_MINUTES_ACTIVE])
                        last_updated = _pick(body, *FALLBACK_KEYS[K_LAST_UPDATED])
                        device_name = _pick(body, *DEVICE_NAME_KEYS)
                        return {
                            K_FILTER_HEALTH: filter_health,
                            K_MINUTES_ACTIVE: minutes_active,
                            K_LAST_UPDATED: last_updated,
                            "device_name": device_name,
                        }

                if resp.status >= 400:
                    raise RuntimeError(f"Status POST {url} -> {resp.status} {text[:500]}")
                data = await resp.json()
        except Exception as e:
            _LOGGER.error("SmartFilterPro status fetch failed: %s", e)
            raise

        body = data.get("response") if isinstance(data, dict) else data
        if not isinstance(body, dict):
            raise RuntimeError(f"Unexpected JSON shape: {body!r}")

        # pull values (telemetry + device_name)
        filter_health = _pick(body, *FALLBACK_KEYS[K_FILTER_HEALTH])
        minutes_active = _pick(body, *FALLBACK_KEYS[K_MINUTES_ACTIVE])
        last_updated = _pick(body, *FALLBACK_KEYS[K_LAST_UPDATED])
        device_name = _pick(body, *DEVICE_NAME_KEYS)

        # Expose device_name so entities can use it for device_info
        return {
            K_FILTER_HEALTH: filter_health,
            K_MINUTES_ACTIVE: minutes_active,
            K_LAST_UPDATED: last_updated,
            "device_name": device_name,
        }

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    coord = SfpStatusCoordinator(hass, entry)
    try:
        await coord.async_config_entry_first_refresh()
    except Exception:
        pass

    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["status_coord"] = coord

    hvac = _normalize_hvac(entry.data.get(CONF_HVAC_UID) or entry.data.get(CONF_HVAC_ID)) or "default"
    suffix = f"{entry.entry_id}_{hvac}"

    entities = [
        SfpFieldSensor(coord, K_FILTER_HEALTH, "SmartFilterPro Filter Health", "%", True,
                       unique_id=f"{DOMAIN}_{suffix}_filter_health"),
        SfpFieldSensor(coord, K_MINUTES_ACTIVE, "SmartFilterPro Minutes Active", "min", True,
                       unique_id=f"{DOMAIN}_{suffix}_minutes_active"),
        SfpFieldSensor(coord, K_LAST_UPDATED, "SmartFilterPro Last Updated", None, False,
                       unique_id=f"{DOMAIN}_{suffix}_last_updated"),
    ]
    async_add_entities(entities)

class SfpFieldSensor(CoordinatorEntity[SfpStatusCoordinator], SensorEntity):
    def __init__(self, coordinator, field_key, name, unit, round_1: bool, *, unique_id: str):
        super().__init__(coordinator)
        self._key = field_key
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = unit
        self._round_1 = round_1

        # Optional: state_class & device_class
        if field_key == K_FILTER_HEALTH:
            self._attr_state_class = "measurement"
            self._attr_device_class = "power_factor"
        elif field_key == K_MINUTES_ACTIVE:
            self._attr_state_class = "measurement"
            self._attr_device_class = "duration"
        elif field_key == K_LAST_UPDATED:
            # Last updated is a date string, no state_class or device_class
            self._attr_state_class = None

    @property
    def device_info(self):
        entry = self.coordinator.entry
        hass = self.coordinator.hass

        # Prefer Bubble name from the coordinator
        bubble_name = None
        if isinstance(self.coordinator.data, dict):
            bubble_name = self.coordinator.data.get("device_name")

        # Fallback to HA climate entity name
        if not bubble_name:
            climate_eid = entry.data.get(CONF_CLIMATE_ENTITY_ID)
            if climate_eid:
                st = hass.states.get(climate_eid)
                if st and getattr(st, "name", None):
                    bubble_name = st.name

        device_name = f"SmartFilterPro — {bubble_name}" if bubble_name else "SmartFilterPro"
        ident = f"{entry.entry_id}:{entry.data.get(CONF_CLIMATE_ENTITY_ID) or 'default'}"

        return {
            "identifiers": {(DOMAIN, ident)},
            "manufacturer": "SmartFilterPro",
            "model": "Filter telemetry bridge",
            "name": device_name,
        }

    @property
    def native_value(self):
        val = (self.coordinator.data or {}).get(self._key)
        if self._round_1 and isinstance(val, (int, float)):
            try:
                return round(float(val), 1)
            except Exception:
                return val
        return val
