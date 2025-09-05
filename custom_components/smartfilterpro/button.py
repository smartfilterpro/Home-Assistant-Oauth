from __future__ import annotations

import aiohttp, asyncio, json, logging, time
from typing import Optional, Any, Iterable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    CONF_API_BASE, CONF_RESET_PATH, CONF_USER_ID, CONF_HVAC_ID,
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT, CONF_REFRESH_PATH,
    CONF_CLIMATE_ENTITY_ID,
    DEFAULT_RESET_PATH, DEFAULT_REFRESH_PATH, TOKEN_SKEW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

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

async def _ensure_valid_token(hass: HomeAssistant, entry: ConfigEntry) -> Optional[str]:
    exp = entry.data.get(CONF_EXPIRES_AT)
    exp = int(exp) if exp is not None else None
    if exp is not None and int(time.time()) >= exp - TOKEN_SKEW_SECONDS:
        rt = entry.data.get(CONF_REFRESH_TOKEN)
        if rt:
            api_base = (entry.data.get(CONF_API_BASE) or "").rstrip("/")
            refresh_path = (entry.data.get(CONF_REFRESH_PATH) or DEFAULT_REFRESH_PATH).strip("/")
            url = f"{api_base}/{refresh_path}"
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(url, json={"refresh_token": rt}, timeout=20) as r:
                        text = await r.text()
                        if r.status < 400:
                            data = json.loads(text)
                            body = data.get("response", data) if isinstance(data, dict) else {}
                            at = body.get("access_token")
                            new_rt = body.get("refresh_token", rt)
                            new_exp = body.get("expires_at")
                            if at and new_exp is not None:
                                new_data = dict(entry.data)
                                new_data.update({"access_token": at, "refresh_token": new_rt, "expires_at": int(new_exp)})
                                hass.config_entries.async_update_entry(entry, data=new_data)
            except Exception as e:
                _LOGGER.error("Refresh before reset failed: %s", e)
    return (hass.config_entries.async_get_entry(entry.entry_id).data).get(CONF_ACCESS_TOKEN)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    async_add_entities([SmartFilterProResetButton(hass, entry)], True)

class SmartFilterProResetButton(ButtonEntity):
    _attr_name = "Reset Filter Usage"
    _attr_icon = "mdi:filter-reset"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry

        # Unique id can keep hvac id for uniqueness
        raw = entry.data.get(CONF_HVAC_ID, "unknown")
        hvac_id = _normalize_hvac(raw) or "unknown"
        self._attr_unique_id = f"{DOMAIN}_reset_{entry.entry_id}_{hvac_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Dynamic device name: prefer Bubble's device_name, fallback to HA climate name."""
        climate_eid = self.entry.data.get(CONF_CLIMATE_ENTITY_ID)

        # Pull latest name from the status coordinator (Bubble)
        coord = (self.hass.data.get(DOMAIN, {})
                            .get(self.entry.entry_id, {})
                            .get("status_coord"))
        bubble_name = None
        if coord and isinstance(coord.data, dict):
            bubble_name = coord.data.get("device_name")

        # Fallback to HA climate friendly name
        friendly = None
        if not bubble_name and climate_eid:
            st = self.hass.states.get(climate_eid)
            if st and getattr(st, "name", None):
                friendly = st.name

        device_name = f"SmartFilterPro — {bubble_name or friendly}" if (bubble_name or friendly) else "SmartFilterPro"
        ident = f"{self.entry.entry_id}:{climate_eid or 'default'}"

        return DeviceInfo(
            identifiers={(DOMAIN, ident)},
            name=device_name,
            manufacturer="SmartFilterPro",
            model="Filter telemetry bridge",
        )

    async def async_press(self) -> None:
        api_base = (self.entry.data.get(CONF_API_BASE) or "").rstrip("/")
        reset_path = (self.entry.data.get(CONF_RESET_PATH) or DEFAULT_RESET_PATH).strip("/")
        user_id = self.entry.data.get(CONF_USER_ID)
        hvac_id = _normalize_hvac(self.entry.data.get(CONF_HVAC_ID))

        if not api_base or not user_id or not hvac_id:
            _LOGGER.error("Reset aborted: missing api_base/user_id/hvac_id")
            return

        token = await _ensure_valid_token(self.hass, self.entry)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url = f"{api_base}/{reset_path}"
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
            coord = (self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("status_coord"))
            if coord:
                await coord.async_request_refresh()
                async def _later():
                    try:
                        await asyncio.sleep(3)
                        await coord.async_request_refresh()
                    except Exception:
                        pass
                asyncio.create_task(_later())
