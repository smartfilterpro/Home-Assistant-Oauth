"""Sign in with SmartFilterPro, through Home Assistant's real OAuth flow.

hub_token is mocked; everything else (the flow manager, the OAuth helper,
state signing, unique ids) is Home Assistant's own code.
"""
from __future__ import annotations

import base64
import hashlib
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartfilterpro.const import DOMAIN

PROD_TOKEN_URL = "https://smartfilterpro.com/api/1.1/wf/hub_token"
DEV_TOKEN_URL = "https://smartfilterpro.com/version-test/api/1.1/wf/hub_token"
REDIRECT = "https://my.home-assistant.io/redirect/oauth"

TOKENS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_in": 3600,
    "user_id": "user-apple",
    "hvac_id": ["hv-1"],
    "hvac_name": ["Living Room"],
    "core_token": "core-1",
    "core_token_expires_at": 1795000000123,  # milliseconds, as Bubble often sends
}


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


@pytest.fixture(autouse=True)
def no_setup():
    """Creating the entry must not start polling or talk to Core."""
    with patch("custom_components.smartfilterpro.async_setup_entry", return_value=True):
        yield


@pytest.fixture(autouse=True)
def my_home_assistant(hass: HomeAssistant):
    """The default configuration includes My Home Assistant, which picks the redirect."""
    hass.config.components.add("my")


async def start_sign_in(hass: HomeAssistant, implementation: str = DOMAIN) -> tuple[str, dict]:
    """Start the flow, pick the environment, return (flow_id, hub_connect query)."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pick_implementation"
    assert set(result["data_schema"].schema["implementation"].container) == {DOMAIN, f"{DOMAIN}_dev"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"implementation": implementation})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    query = {k: v[0] for k, v in parse_qs(urlparse(result["url"]).query).items()}
    return result["flow_id"], query | {"_url": result["url"]}


async def come_back(hass: HomeAssistant, flow_id: str, query: dict, code: str = "code-1"):
    """What /auth/external/callback does when SmartFilterPro redirects back."""
    state = config_entry_oauth2_flow._decode_jwt(hass, query["state"])
    await hass.config_entries.flow.async_configure(flow_id, {"code": code, "state": state})
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_hub_connect_link(hass: HomeAssistant):
    flow_id, q = await start_sign_in(hass)
    assert q["_url"].startswith("https://smartfilterpro.com/hub_connect?")
    assert q["client"] == "home_assistant"
    assert q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256"
    assert len(q["code_challenge"]) == 43
    state = config_entry_oauth2_flow._decode_jwt(hass, q["state"])
    assert state["flow_id"] == flow_id and state["redirect_uri"] == REDIRECT
    # state survives Bubble's three find & replace steps untouched (JWT is URL-safe)
    assert not set(q["state"]) & set("/?=")


async def test_sign_in_creates_entry_with_tokens(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.post(PROD_TOKEN_URL, json={"status": "success", "response": TOKENS})
    flow_id, q = await start_sign_in(hass)
    result = await come_back(hass, flow_id, q)

    # The swap: JSON with the code, the verifier behind the challenge, the redirect and client
    assert aioclient_mock.call_count == 1
    sent = aioclient_mock.mock_calls[0][2]
    assert sent["code"] == "code-1" and sent["client"] == "home_assistant" and sent["redirect_uri"] == REDIRECT
    assert s256(sent["code_verifier"]) == q["code_challenge"]
    assert sent["code_verifier"] not in q["_url"]

    # One thermostat: straight to the optional climate entity
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "climate"
    result = await hass.config_entries.flow.async_configure(flow_id, {"climate_entity_id": "__SFP_SKIP__"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SmartFilterPro — Living Room"
    data = result["data"]
    assert data["access_token"] == "access-1" and data["refresh_token"] == "refresh-1"
    assert data["user_id"] == "user-apple" and data["hvac_id"] == "hv-1"
    assert data["environment"] == "production"
    assert data["core_ingest_url"] == "https://core.smartfilterpro.com/ingest/v1/events:batch"
    assert data["core_token"] == "core-1" and data["core_token_exp"] == 1795000000
    assert isinstance(data["expires_at"], int) and data["expires_at"] > 1_700_000_000
    assert "token" not in data and "email" not in data and "password" not in data
    assert result["result"].unique_id == "user-apple_hv-1"


async def test_dev_environment_uses_version_test(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.post(DEV_TOKEN_URL, json={"status": "success", "response": TOKENS})
    flow_id, q = await start_sign_in(hass, f"{DOMAIN}_dev")
    assert q["_url"].startswith("https://smartfilterpro.com/version-test/hub_connect?")
    await come_back(hass, flow_id, q)
    result = await hass.config_entries.flow.async_configure(flow_id, {"climate_entity_id": "__SFP_SKIP__"})
    assert result["data"]["environment"] == "dev"
    assert result["data"]["core_ingest_url"] == "https://core-ingest-dev.up.railway.app/ingest/v1/events:batch"


async def test_several_thermostats_asks_which(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.post(PROD_TOKEN_URL, json={"response": TOKENS | {"hvac_id": ["hv-1", "hv-2"], "hvac_name": ["Up", "Down"]}})
    flow_id, q = await start_sign_in(hass)
    result = await come_back(hass, flow_id, q)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hvac"
    result = await hass.config_entries.flow.async_configure(flow_id, {"hvac_id": "hv-2"})
    assert result["step_id"] == "climate"
    result = await hass.config_entries.flow.async_configure(flow_id, {"climate_entity_id": "__SFP_SKIP__"})
    assert result["data"]["hvac_id"] == "hv-2" and result["data"]["hvac_name"] == "Down"


async def test_refused_code_aborts_without_an_entry(hass: HomeAssistant, aioclient_mock, caplog):
    aioclient_mock.post(PROD_TOKEN_URL, json={"status": "success", "response": {"error": "invalid_grant"}})
    flow_id, q = await start_sign_in(hass)
    result = await come_back(hass, flow_id, q)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "oauth_failed"
    assert "invalid_grant" in caplog.text
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_bubble_error_status_aborts(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.post(PROD_TOKEN_URL, status=400, json={"status": "NOT_RUN"})
    flow_id, q = await start_sign_in(hass)
    result = await come_back(hass, flow_id, q)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "oauth_failed"


async def test_no_thermostats(hass: HomeAssistant, aioclient_mock):
    aioclient_mock.post(PROD_TOKEN_URL, json={"response": TOKENS | {"hvac_id": [], "hvac_name": []}})
    flow_id, q = await start_sign_in(hass)
    result = await come_back(hass, flow_id, q)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_thermostats_found"


async def test_same_thermostat_twice_is_refused(hass: HomeAssistant, aioclient_mock):
    MockConfigEntry(domain=DOMAIN, unique_id="user-apple_hv-1", data={}).add_to_hass(hass)
    aioclient_mock.post(PROD_TOKEN_URL, json={"response": TOKENS})
    flow_id, q = await start_sign_in(hass)
    result = await come_back(hass, flow_id, q)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_tokens_are_never_logged(hass: HomeAssistant, aioclient_mock, caplog):
    caplog.set_level("DEBUG")
    aioclient_mock.post(PROD_TOKEN_URL, json={"response": TOKENS})
    flow_id, q = await start_sign_in(hass)
    await come_back(hass, flow_id, q)
    await hass.config_entries.flow.async_configure(flow_id, {"climate_entity_id": "__SFP_SKIP__"})
    for secret in ("access-1", "refresh-1", "core-1", "code-1"):
        assert secret not in caplog.text


async def test_cancelled_on_smartfilterpro(hass: HomeAssistant):
    flow_id, q = await start_sign_in(hass)
    state = config_entry_oauth2_flow._decode_jwt(hass, q["state"])
    await hass.config_entries.flow.async_configure(flow_id, {"error": "access_denied", "state": state})
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "user_rejected_authorize"
