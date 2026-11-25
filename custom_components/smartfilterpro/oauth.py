"""Sign in with SmartFilterPro: OAuth authorization code + PKCE.

Home Assistant's own OAuth helper (config_entry_oauth2_flow) runs the flow:
it opens SmartFilterPro's hub_connect page, where the user signs in however
they signed up (Apple, Google or email), and it receives the one-time code on
https://my.home-assistant.io/redirect/oauth (or this instance's
/auth/external/callback when My Home Assistant is off). This module supplies
what is SmartFilterPro-specific:

  - the hub_connect / hub_token addresses for each environment;
  - PKCE (S256): a verifier made per flow, kept in memory until the swap and
    never sent anywhere but hub_token;
  - the swap itself: hub_token takes JSON (code, code_verifier,
    redirect_uri, client) and answers like Bubble always does, wrapped in
    {"response": {...}}, with {"error": "invalid_grant"} when it refuses.

Token refresh after setup stays where it was (auth.py -> ha_refresh_token),
so config entries keep the same shape whichever way they were created.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .const import (
    DOMAIN,
    ENVIRONMENTS,
    ENV_PRODUCTION,
    HUB_CONNECT_PATH,
    HUB_TOKEN_PATH,
    OAUTH_CLIENT_ID,
)

_LOGGER = logging.getLogger(__name__)

# hub_token returns expires_in; Home Assistant's helper requires it. If an
# older workflow leaves it out, assume an hour: auth.py refreshes early anyway.
DEFAULT_EXPIRES_IN = 3600

IMPLEMENTATION_NAMES = {
    ENV_PRODUCTION: "SmartFilterPro",
}


def implementation_domain(environment: str) -> str:
    """Key Home Assistant stores the implementation under (one per environment)."""
    return DOMAIN if environment == ENV_PRODUCTION else f"{DOMAIN}_{environment}"


def code_challenge(verifier: str) -> str:
    """PKCE S256: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_code_verifier() -> str:
    """64 random URL-safe characters (PKCE allows 43-128)."""
    return secrets.token_urlsafe(48)


class SmartFilterProImplementation(config_entry_oauth2_flow.LocalOAuth2Implementation):
    """hub_connect / hub_token for one SmartFilterPro environment."""

    def __init__(self, hass: HomeAssistant, environment: str) -> None:
        base = ENVIRONMENTS[environment]["api_base"].rstrip("/")
        super().__init__(
            hass,
            implementation_domain(environment),
            OAUTH_CLIENT_ID,
            "",  # no client secret: the integration's code is public, PKCE protects the code
            f"{base}{HUB_CONNECT_PATH}",
            f"{base}{HUB_TOKEN_PATH}",
        )
        self.environment = environment
        self._verifiers: dict[str, str] = {}

    @property
    def name(self) -> str:
        return IMPLEMENTATION_NAMES.get(self.environment, f"SmartFilterPro ({self.environment} / test)")

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """hub_connect address: Home Assistant's state and redirect, plus client and PKCE."""
        url = await super().async_generate_authorize_url(flow_id)
        verifier = new_code_verifier()
        self._verifiers[flow_id] = verifier
        return str(
            URL(url).update_query(
                {
                    "client": OAUTH_CLIENT_ID,
                    "code_challenge": code_challenge(verifier),
                    "code_challenge_method": "S256",
                }
            )
        )

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Swap the one-time code at hub_token for this installation's tokens.

        Raises aiohttp.ClientError on any refusal; Home Assistant's flow turns
        that into an "oauth_failed" abort. Nothing returned by hub_token is
        logged: on success it holds tokens.
        """
        state = external_data.get("state") or {}
        verifier = self._verifiers.pop(state.get("flow_id"), None)
        if not verifier:
            raise aiohttp.ClientError("this sign-in was started before Home Assistant restarted; start it again")

        payload = {
            "code": external_data["code"],
            "code_verifier": verifier,
            "redirect_uri": state.get("redirect_uri"),
            "client": OAUTH_CLIENT_ID,
        }
        session = async_get_clientsession(self.hass)
        async with session.post(self.token_url, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientError(f"hub_token answered HTTP {resp.status}")
            try:
                data = await resp.json(content_type=None)
            except ValueError as err:
                raise aiohttp.ClientError("hub_token answered with something other than JSON") from err

        body = data.get("response", data) if isinstance(data, dict) else {}
        if not isinstance(body, dict) or not body.get("access_token"):
            reason = body.get("error") if isinstance(body, dict) else None
            raise aiohttp.ClientError(f"SmartFilterPro did not accept the sign-in ({reason or 'no tokens returned'})")

        token = dict(body)
        if token.get("expires_in") in (None, ""):
            token["expires_in"] = DEFAULT_EXPIRES_IN
        return token

    async def _async_refresh_token(self, token: dict) -> dict:
        # Not used: auth.py refreshes through ha_refresh_token and keeps the
        # tokens in the config entry's data, as it always has.
        raise NotImplementedError("SmartFilterPro tokens are refreshed by auth.py")


def async_register_implementations(hass: HomeAssistant) -> None:
    """One sign-in option per environment; Home Assistant asks which when there are several."""
    for environment in ENVIRONMENTS:
        config_entry_oauth2_flow.async_register_implementation(
            hass, DOMAIN, SmartFilterProImplementation(hass, environment)
        )
