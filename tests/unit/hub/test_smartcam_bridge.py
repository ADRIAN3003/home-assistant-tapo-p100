from unittest.mock import patch

from kasa import AuthenticationError
from plugp100.common.credentials import AuthCredential
from plugp100.components.hub_children import HubChildrenComponent
from plugp100.components.smart_door import SmartDoorComponent
from plugp100.components.trigger_log import TriggerLogComponent
from plugp100.devices.children.door import SmartDoorSensor
from plugp100.devices.children.trigger_button import TriggerButtonDevice
from plugp100.devices.hub import TapoHub
from plugp100.discovery import DiscoveredDevice
from plugp100.errors import InvalidAuthentication, TapoException
from plugp100.models.hub_children.button import (
    DoubleClickEvent,
    RotationEvent,
    SingleClickEvent,
)
import pytest

from custom_components.tapo.hub.smartcam_bridge import (
    connect_smartcam_hub,
    is_smartcam_hub,
)
from tests.unit.hub.smartcam_fake import (
    BUTTON_ID,
    CAMERA_ID,
    DOOR_ID,
    HUB_DEVICE_ID,
    HUB_MAC,
    FakeSmartCamProtocol,
)


def _discovered(device_type: str, **encrypt_schema) -> DiscoveredDevice:
    return DiscoveredDevice.from_dict(
        {
            "device_type": device_type,
            "device_model": "H200",
            "ip": "192.168.1.61",
            "mac": HUB_MAC,
            "mgt_encrypt_schm": encrypt_schema,
        }
    )


class TestIsSmartCamHub:
    def test_h200_discovery(self):
        assert is_smartcam_hub(_discovered("SMART.TAPOHUB", is_support_https=True))

    def test_h100_discovery(self):
        assert not is_smartcam_hub(
            _discovered(
                "SMART.TAPOHUB", is_support_https=False, encrypt_type="KLAP", lv=2
            )
        )

    def test_plug_discovery(self):
        assert not is_smartcam_hub(
            _discovered("SMART.TAPOPLUG", is_support_https=True, encrypt_type="KLAP")
        )

    def test_none(self):
        assert not is_smartcam_hub(None)


@pytest.fixture
def fake_protocol() -> FakeSmartCamProtocol:
    return FakeSmartCamProtocol()


@pytest.fixture
async def hub(fake_protocol: FakeSmartCamProtocol) -> TapoHub:
    with patch(
        "custom_components.tapo.hub.smartcam_bridge.get_protocol",
        return_value=fake_protocol,
    ):
        hub = await connect_smartcam_hub(
            "192.168.1.61", AuthCredential("user@example.com", "secret")
        )
    await hub.update()
    return hub


def _child(hub: TapoHub, device_id: str):
    return next(child for child in hub.children if child.device_id == device_id)


class TestSmartCamHub:
    async def test_hub_info_is_translated(self, hub: TapoHub):
        assert hub.device_id == HUB_DEVICE_ID
        assert hub.model == "H200"
        assert hub.nickname == "Living room hub"
        assert hub.mac == HUB_MAC
        assert hub.firmware_version == "1.3.6"
        assert hub.device_info.hardware_version == "1.0"
        assert hub.protocol_version == "SmartCam"
        assert hub.has_component(HubChildrenComponent)
        assert not hub.has_alarm

    async def test_only_smart_children_are_created(self, hub: TapoHub):
        assert {child.device_id for child in hub.children} == {BUTTON_ID, DOOR_ID}
        button = _child(hub, BUTTON_ID)
        assert isinstance(button, TriggerButtonDevice)
        assert button.has_component(TriggerLogComponent)
        assert button.nickname == "Desk button"
        door = _child(hub, DOOR_ID)
        assert isinstance(door, SmartDoorSensor)
        assert door.get_component(SmartDoorComponent).is_open is True

    async def test_child_device_ids_skip_cameras(self, hub: TapoHub):
        child_list = (await hub.client.get_child_device_list()).get_or_raise()
        assert child_list.get_device_ids() == {BUTTON_ID, DOOR_ID}
        assert CAMERA_ID not in child_list.get_device_ids()

    async def test_button_events_are_tunnelled(
        self, hub: TapoHub, fake_protocol: FakeSmartCamProtocol
    ):
        fake_protocol.push_event("singleClick")
        fake_protocol.push_event("rotation", rotate_deg=15)
        fake_protocol.push_event("rotation", rotate_deg=-30)
        fake_protocol.push_event("doubleClick")

        button = _child(hub, BUTTON_ID)
        logs = (await button.get_event_logs(page_size=5, start_id=0)).get_or_raise()

        assert [type(event) for event in logs.events] == [
            DoubleClickEvent,
            RotationEvent,
            RotationEvent,
            SingleClickEvent,
        ]
        assert [e.degrees for e in logs.events if isinstance(e, RotationEvent)] == [
            -30,
            15,
        ]
        assert fake_protocol.child_requests(BUTTON_ID)[-1]["request_data"] == {
            "method": "get_trigger_logs",
            "params": {"page_size": 5, "start_id": 0},
        }

    async def test_child_error_is_reported(
        self, hub: TapoHub, fake_protocol: FakeSmartCamProtocol
    ):
        fake_protocol.child_error_code = -1301
        result = await _child(hub, BUTTON_ID).get_event_logs(page_size=5)
        assert result.is_failure()
        assert isinstance(result.error(), TapoException)
        assert result.error().error_code == -1301

    async def test_authentication_error(
        self, hub: TapoHub, fake_protocol: FakeSmartCamProtocol
    ):
        async def _fail(*_args, **_kwargs):
            raise AuthenticationError("bad credentials")

        fake_protocol.query = _fail
        with pytest.raises(InvalidAuthentication):
            await hub.update()

    async def test_unsupported_request_fails(self, hub: TapoHub):
        result = await hub.get_latest_firmware()
        assert result.is_failure()

    async def test_close_closes_kasa_protocol(
        self, hub: TapoHub, fake_protocol: FakeSmartCamProtocol
    ):
        await hub.client.close()
        assert fake_protocol.closed
