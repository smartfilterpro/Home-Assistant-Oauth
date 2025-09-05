from __future__ import annotations

import json
import logging
import time
from datetime import timedelta, datetime
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

_LOGGER = logging.getLogger(__name__)

K_PERCENT = "percentage_used"
K_TODAY   = "today_minutes"
K_TOTAL   = "total_minutes"

FALLBACK_KEYS = {
    K_PERCENT: ("percentage", "percent_used", "percentage used"),
    K_TODAY:   ("today", "todays_minutes", "2.0.1_Daily Active Time Sum"),
    K_TOTAL:   ("total", "total_runtime", "1.0.1_Minutes active"),
}

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

    # --- token helpers ---
    def _access_token(self) -> Optional[str]:
        tok = self.entry.data.get(CONF_ACCESS_TOKEN) or self.entry.data.get("token") or self.entry.data.get("id_token")
        return tok

    def _refresh_token(self) -> Optional[str]:
        return self.entry.data.get(CONF_REFRESH_TOKEN)

    def _expires_at(self) -> Optional[int]:
        v = self.entry.data.get(CONF_EXPIRES_AT)
        return int(v) if v is not None else None

    async def _ensure_valid_token(self) -> None:
        exp = self._expires_at()
        if exp is None:
            return
        if int(time.time()) < exp - TOKEN_SKEW_SECONDS:
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
        at = _pick(body, "access_token", "token", "id_token")
        new_rt = _pick(body, "refresh_token", "rtoken") or rt
        exp = _pick(body, "expires_at")

        if not at or exp is None:
            _LOGGER.error("Refresh response missing access_token/expires_at: %s", body)
            return

        new_data = dict(self.entry.data)
        new_data.update({"access_token": at, "refresh_token": new_rt, "expires_at": int(exp)})
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        _LOGGER.debug("Token refreshed; exp=%s", exp)

    # --- main poll ---
    async def _async_update_data(self) -> dict:
        await self._ensure_valid_token()
        token = self._access_token()

        raw_hvac = self.entry.data.get(CONF_HVAC_UID) or self.entry.data.get(CONF_HVAC_ID)
        hvac_uid = _normalize_hvac(raw_hvac)
        user_id  = self.entry.data.get(CONF_USER_ID)

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
        percent = _pick(body, K_PERCENT, *FALLBACK_KEYS[K_PERCENT])
        today   = _pick(body, K_TODAY,   *FALLBACK_KEYS[K_TODAY])
        total   = _pick(body, K_TOTAL,   *FALLBACK_KEYS[K_TOTAL])
        device_name = _pick(body, "device_name", "thermostat_name", "name")

        # Expose device_name so entities can use it for device_info
        return {
            K_PERCENT: percent,
            K_TODAY: today,
            K_TOTAL: total,
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
        SfpFieldSensor(coord, K_PERCENT, "SmartFilterPro Percentage Used", "%", True,
                       unique_id=f"{DOMAIN}_{suffix}_percentage_used"),
        SfpFieldSensor(coord, K_TODAY,   "SmartFilterPro Today's Usage",   "min", False,
                       unique_id=f"{DOMAIN}_{suffix}_todays_usage"),
        SfpFieldSensor(coord, K_TOTAL,   "SmartFilterPro Total Minutes",   "min", False,
                       unique_id=f"{DOMAIN}_{suffix}_total_minutes"),
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
        self._attr_state_class = "measurement"
        if field_key in ("today_minutes", "total_minutes"):
            self._attr_device_class = "duration"
        elif field_key == "percentage_used":
            self._attr_device_class = "power_factor"

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
                return round(float(val), 1)  # change to 2 if you want 0.00
            except Exception:
                return val
        return val
