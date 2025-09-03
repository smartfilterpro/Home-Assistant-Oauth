# custom_components/smartfilterpro/application_credentials.py
from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant

DOMAIN = "smartfilterpro"

async def async_get_client_credential(hass: HomeAssistant) -> ClientCredential | None:
    # HA will prompt the user in UI to paste the client id/secret for SmartFilterPro.
    # Nothing to hardcode here; this hook just tells HA we support app credentials.
    return None
