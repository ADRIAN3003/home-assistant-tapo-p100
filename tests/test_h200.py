"""Tests for Tapo hubs reached through the smartcam bridge (e.g. H200)."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from plugp100.discovery import DiscoveredDevice
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tapo.const import (
    CONF_DISCOVERED_DEVICE_INFO,
    CONF_HOST,
    CONF_MAC,
    DEFAULT_POLLING_RATE_S,
    DOMAIN,
    STEP_DISCOVERY_REQUIRE_AUTH,
)
from custom_components.tapo.hub.tapo_cloud import TapoCloudEventLog
from tests.unit.hub.smartcam_fake import (
    BUTTON_ID,
    CAMERA_ID,
    HUB_DEVICE_ID,
    HUB_MAC,
    FakeSmartCamProtocol,
    FakeTapoCloudClient,
)

H200_IP = "192.168.1.61"


def _h200_discovery() -> DiscoveredDevice:
    return DiscoveredDevice.from_dict(
        {
            "device_type": "SMART.TAPOHUB",
            "device_model": "H200",
            "ip": H200_IP,
            "mac": HUB_MAC,
            "mgt_encrypt_schm": {"is_support_https": True, "lv": 1},
        }
    )


@pytest.fixture
def fake_protocol():
    fake = FakeSmartCamProtocol()
    with (
        patch(
            "custom_components.tapo.hub.smartcam_bridge.get_protocol",
            return_value=fake,
        ),
        patch(
            "custom_components.tapo.hub.smartcam_bridge.TapoCloudClient",
            side_effect=lambda *_args: FakeTapoCloudClient(fake),
        ),
        patch.object(TapoCloudEventLog, "MIN_FETCH_INTERVAL_S", 0),
    ):
        yield fake


async def test_discovered_h200_uses_smartcam_bridge(
    hass: HomeAssistant, fake_protocol: FakeSmartCamProtocol
) -> None:
    discovered = _h200_discovery()
    with (
        patch(
            "custom_components.tapo.config_flow.connect_discovered_device",
            AsyncMock(side_effect=AssertionError("plugp100 connect must not be used")),
        ),
        patch("custom_components.tapo.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data={
                CONF_HOST: H200_IP,
                CONF_MAC: dr.format_mac(HUB_MAC),
                CONF_SCAN_INTERVAL: DEFAULT_POLLING_RATE_S,
                CONF_DISCOVERED_DEVICE_INFO: discovered.as_dict,
            },
        )
        assert result["step_id"] == STEP_DISCOVERY_REQUIRE_AUTH
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room hub"
    assert result["context"]["unique_id"] == dr.format_mac(HUB_MAC)
    assert result["data"][CONF_DISCOVERED_DEVICE_INFO] == discovered.as_dict


async def test_manual_h200_falls_back_to_smartcam_bridge(
    hass: HomeAssistant, fake_protocol: FakeSmartCamProtocol
) -> None:
    discovered = _h200_discovery()
    with (
        patch(
            "custom_components.tapo.config_flow.connect",
            AsyncMock(side_effect=Exception("protocol detection timed out")),
        ),
        patch(
            "custom_components.tapo.config_flow.discover_tapo_device",
            AsyncMock(return_value=discovered),
        ),
        patch("custom_components.tapo.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_HOST: H200_IP,
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "secret",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == H200_IP
    assert result["data"][CONF_DISCOVERED_DEVICE_INFO] == discovered.as_dict


async def test_manual_non_hub_failure_is_not_masked(hass: HomeAssistant) -> None:
    with (
        patch(
            "custom_components.tapo.config_flow.connect",
            AsyncMock(side_effect=Exception("unreachable")),
        ),
        patch(
            "custom_components.tapo.config_flow.discover_tapo_device",
            AsyncMock(return_value=None),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_HOST: "1.2.3.4",
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "secret",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_h200_entry_setup_creates_child_entities(
    hass: HomeAssistant, fake_protocol: FakeSmartCamProtocol
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: H200_IP,
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_SCAN_INTERVAL: DEFAULT_POLLING_RATE_S,
            CONF_DISCOVERED_DEVICE_INFO: _h200_discovery().as_dict,
        },
        version=8,
        unique_id=dr.format_mac(HUB_MAC),
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.tapo.hass_tapo.connect_discovered_device",
        AsyncMock(side_effect=AssertionError("plugp100 connect must not be used")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_get(hass)
    hub_device = devices.async_get_device(identifiers={(DOMAIN, HUB_DEVICE_ID)})
    assert hub_device is not None and hub_device.model == "H200"
    assert devices.async_get_device(identifiers={(DOMAIN, BUTTON_ID)}) is not None
    assert devices.async_get_device(identifiers={(DOMAIN, CAMERA_ID)}) is None

    entities = er.async_get(hass)
    for unique_id, platform in [
        (f"{BUTTON_ID}_button_event", "event"),
        (f"{BUTTON_ID}_dial_event", "event"),
    ]:
        assert entities.async_get_entity_id(platform, DOMAIN, unique_id), unique_id
    assert hass.states.get("binary_sensor.front_door").state == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_protocol.closed


async def test_h200_button_actions_fire_events(
    hass: HomeAssistant, fake_protocol: FakeSmartCamProtocol
) -> None:
    fake_protocol.push_event("singleClick")  # history already on the device
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: H200_IP,
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_SCAN_INTERVAL: DEFAULT_POLLING_RATE_S,
            CONF_DISCOVERED_DEVICE_INFO: _h200_discovery().as_dict,
        },
        version=8,
        unique_id=dr.format_mac(HUB_MAC),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    button_coordinator = next(
        coordinator
        for coordinator in hass.data[DOMAIN][entry.entry_id].child_coordinators
        if coordinator.device.device_id == BUTTON_ID
    )

    async def _poll() -> None:
        button_coordinator._event_log_cache = None  # polls are >300ms apart for real
        await button_coordinator.async_refresh()
        await hass.async_block_till_done()

    await _poll()  # first poll only records the latest known event

    fake_protocol.push_event("rotation", rotate_deg=-15)
    await _poll()
    dial = hass.states.get("event.desk_button_dial_event")
    assert dial.attributes["event_type"] == "rotation"
    assert dial.attributes["degrees"] == -15

    fake_protocol.push_event("singleClick")
    await _poll()
    button = hass.states.get("event.desk_button_button_event")
    assert button.attributes["event_type"] == "single_click"

    fake_protocol.push_event("singleClick")
    fake_protocol.push_event("singleClick")
    await _poll()
    button = hass.states.get("event.desk_button_button_event")
    assert button.attributes["event_type"] == "double_click"

    # the H200 has no local trigger logs: events must come from the Tapo cloud
    assert all(
        request["request_data"]["method"] != "get_trigger_logs"
        for request in fake_protocol.child_requests()
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
