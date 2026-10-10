from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from plugp100.api.tapo_client import TapoClient
from plugp100.devices.hub import TapoHub
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tapo.hub.smartcam_bridge import SmartCamBridgeProtocol
from custom_components.tapo.hub.tapo_cloud import (
    TapoCloudAuthError,
    TapoCloudClient,
    TapoCloudError,
    TapoCloudEventLog,
    merge_double_clicks,
    to_trigger_logs,
)
from tests.unit.hub.smartcam_fake import (
    BUTTON_ID,
    FakeSmartCamProtocol,
    FakeTapoCloudClient,
)

APP_SERVER = "https://euw1-app-server.iot.i.tplinkcloud.com"
LOGIN_URL = "https://eu-wap.tplinkcloud.com"
SERVICE_URL = LOGIN_URL + "/api/v2/common/getAppServiceUrlByCloudUserName"
BUTTON_THING = {"thingName": "BUTTON_ID", "model": "S200B", "mac": "3C-52-A1-00-00-00"}


def _cloud_event(log_id: int, name: str, timestamp: int = 1791583098, **params):
    return {
        "logId": log_id,
        "eventId": f"e{log_id}",
        "name": name,
        "timestamp": timestamp,
        "params": params,
    }


def _log(log_id: int, event: str = "singleClick", timestamp: int = 100) -> dict:
    return {"id": log_id, "timestamp": timestamp, "event": event, "params": {}}


def _summary(logs: list[dict]) -> list[tuple[int, str]]:
    return [(log["id"], log["event"]) for log in logs]


class TestMergeDoubleClicks:
    def test_two_quick_single_clicks_become_double(self):
        # newest first, as returned by the cloud (ids 1785/1786 from a real H200)
        logs = [_log(1786, timestamp=100), _log(1785, timestamp=100)]
        assert _summary(merge_double_clicks(logs, {})) == [(1786, "doubleClick")]

    def test_one_second_apart_is_still_double(self):
        logs = [_log(2, timestamp=101), _log(1, timestamp=100)]
        assert _summary(merge_double_clicks(logs, {})) == [(2, "doubleClick")]

    def test_slow_clicks_stay_single(self):
        logs = [_log(2, timestamp=102), _log(1, timestamp=100)]
        assert _summary(merge_double_clicks(logs, {})) == [
            (2, "singleClick"),
            (1, "singleClick"),
        ]

    def test_triple_click_is_double_plus_single(self):
        logs = [_log(3), _log(2), _log(1)]
        assert _summary(merge_double_clicks(logs, {})) == [
            (3, "singleClick"),
            (2, "doubleClick"),
        ]

    def test_rotation_in_between_breaks_the_pair(self):
        logs = [_log(3), _log(2, "rotation"), _log(1)]
        assert _summary(merge_double_clicks(logs, {})) == [
            (3, "singleClick"),
            (2, "rotation"),
            (1, "singleClick"),
        ]

    def test_already_returned_click_is_never_merged(self):
        decisions: dict[int, str] = {}
        merge_double_clicks([_log(1)], decisions)
        assert _summary(merge_double_clicks([_log(2), _log(1)], decisions)) == [
            (2, "singleClick"),
            (1, "singleClick"),
        ]

    def test_merges_are_stable_across_fetches(self):
        # the sequence seen on a real H200: two double presses, then more clicks
        decisions: dict[int, str] = {}
        page = [_log(1815, timestamp=936), _log(1814, timestamp=933)]
        page += [_log(1813, timestamp=933), _log(1812, timestamp=929)]
        page += [_log(1811, timestamp=929)]
        first = merge_double_clicks(page, decisions)
        second = merge_double_clicks(
            [_log(1817, timestamp=939), _log(1816, timestamp=938), *page], decisions
        )
        assert _summary(first) == [
            (1815, "singleClick"),
            (1814, "doubleClick"),
            (1812, "doubleClick"),
        ]
        assert _summary(second) == [(1817, "doubleClick"), *_summary(first)]

    def test_forgets_entries_outside_the_page(self):
        decisions: dict[int, str] = {}
        merge_double_clicks([_log(2, timestamp=10), _log(1, timestamp=1)], decisions)
        merge_double_clicks([_log(3, timestamp=20), _log(2, timestamp=10)], decisions)
        assert set(decisions) == {2, 3}


class TestToTriggerLogs:
    def test_converts_to_local_format_newest_first(self):
        logs = to_trigger_logs(
            [
                _cloud_event(1481, "rotation", rotate_deg=-30),
                _cloud_event(1483, "singleClick"),
                _cloud_event(1482, "doubleClick"),
            ]
        )
        assert logs["start_id"] == 1483
        assert logs["sum"] == 3
        assert [(log["id"], log["event"]) for log in logs["logs"]] == [
            (1483, "singleClick"),
            (1482, "doubleClick"),
            (1481, "rotation"),
        ]
        assert logs["logs"][2]["params"] == {"rotate_deg": -30}

    def test_skips_entries_plugp100_cannot_parse(self):
        logs = to_trigger_logs(
            [
                _cloud_event(3, "lowBattery"),
                _cloud_event(2, "rotation"),
                {"name": "singleClick", "logId": "not-a-number"},
                _cloud_event(1, "singleClick"),
            ]
        )
        assert [log["id"] for log in logs["logs"]] == [1]

    def test_empty(self):
        assert to_trigger_logs([]) == {"start_id": 0, "sum": 0, "logs": []}


class FakeClient:
    def __init__(self, things=None, events=None):
        self.things = AsyncMock(return_value=things or [BUTTON_THING])
        self.events = AsyncMock(return_value=events or [])


class TestTapoCloudEventLog:
    async def test_fetches_events_of_matching_thing(self):
        client = FakeClient(events=[_cloud_event(7, "singleClick")])
        logs = await TapoCloudEventLog(client).get_trigger_logs("BUTTON_ID", None, 5)
        assert logs["logs"][0]["id"] == 7
        client.events.assert_awaited_once_with(BUTTON_THING, 20)

    async def test_matches_by_mac_when_thing_name_differs(self):
        client = FakeClient()
        await TapoCloudEventLog(client).get_trigger_logs("LOCAL_ID", "3C52A1000000", 5)
        client.events.assert_awaited_once_with(BUTTON_THING, 20)

    async def test_double_click_split_across_fetches_never_replays(self):
        client = FakeClient(events=[_cloud_event(1, "singleClick", timestamp=100)])
        event_log = TapoCloudEventLog(client)
        with patch.object(TapoCloudEventLog, "MIN_FETCH_INTERVAL_S", 0):
            first = await event_log.get_trigger_logs("BUTTON_ID", None, 5)
            client.events.return_value = [
                _cloud_event(2, "singleClick", timestamp=100),
                _cloud_event(1, "singleClick", timestamp=100),
            ]
            second = await event_log.get_trigger_logs("BUTTON_ID", None, 5)
        assert _summary(first["logs"]) == [(1, "singleClick")]
        assert _summary(second["logs"]) == [(2, "singleClick"), (1, "singleClick")]

    async def test_double_click_in_one_fetch_is_merged(self):
        client = FakeClient(
            events=[
                _cloud_event(3, "singleClick", timestamp=200),
                _cloud_event(2, "singleClick", timestamp=200),
                _cloud_event(1, "rotation", timestamp=100, rotate_deg=30),
            ]
        )
        logs = await TapoCloudEventLog(client).get_trigger_logs("BUTTON_ID", None, 5)
        assert _summary(logs["logs"]) == [(3, "doubleClick"), (1, "rotation")]
        assert logs["sum"] == 2

    async def test_unknown_device(self):
        with pytest.raises(TapoCloudError):
            await TapoCloudEventLog(FakeClient()).get_trigger_logs("OTHER", None, 5)

    async def test_requests_are_throttled_per_button(self):
        client = FakeClient()
        event_log = TapoCloudEventLog(client)
        await event_log.get_trigger_logs("BUTTON_ID", None, 5)
        await event_log.get_trigger_logs("BUTTON_ID", None, 5)
        assert client.events.await_count == 1
        assert client.things.await_count == 1

    async def test_failures_back_off(self):
        client = FakeClient()
        client.things.side_effect = TapoCloudAuthError("bad credentials")
        event_log = TapoCloudEventLog(client)
        with pytest.raises(TapoCloudAuthError):
            await event_log.get_trigger_logs("BUTTON_ID", None, 5)
        with pytest.raises(TapoCloudError, match="retrying"):
            await event_log.get_trigger_logs("BUTTON_ID", None, 5)
        assert client.things.await_count == 1


@pytest.fixture
async def session(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    session = aioclient_mock.create_session(hass.loop)
    yield session
    await session.close()


@pytest.fixture
def cloud(session, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.post(LOGIN_URL, json={"error_code": 0, "result": {"token": "TOKEN"}})
    aioclient_mock.post(
        SERVICE_URL,
        json={
            "error_code": 0,
            "result": {
                "serviceList": [
                    {"serviceId": "nbu.iot-app-server.app-v2", "serviceUrl": APP_SERVER}
                ]
            },
        },
    )
    return TapoCloudClient(session, "user@example.com", "secret")


class TestTapoCloudClient:
    async def test_login_and_read_events(
        self, cloud: TapoCloudClient, aioclient_mock: AiohttpClientMocker
    ):
        aioclient_mock.get(APP_SERVER + "/v2/things", json={"data": [BUTTON_THING]})
        aioclient_mock.get(
            APP_SERVER + "/v1/things/BUTTON_ID/events",
            json={"events": [_cloud_event(1, "singleClick")]},
        )

        assert await cloud.things() == [BUTTON_THING]
        assert await cloud.events(BUTTON_THING, 20) == [_cloud_event(1, "singleClick")]

        login_call, service_call, things_call, events_call = aioclient_mock.mock_calls
        assert login_call[2]["params"]["cloudUserName"] == "user@example.com"
        assert "Signature=" in service_call[3]["X-Authorization"]
        assert things_call[3]["Authorization"] == "ut|TOKEN"
        assert events_call[1].query["fetchSize"] == "20"
        assert events_call[1].query["order"] == "DESC"

    async def test_expired_token_logs_in_again(
        self, cloud: TapoCloudClient, aioclient_mock: AiohttpClientMocker
    ):
        statuses = [HTTPStatus.UNAUTHORIZED, HTTPStatus.OK]

        async def _things(method, url, data):
            return AiohttpClientMockResponse(
                method, URL(url), status=statuses.pop(0), json={"data": []}
            )

        aioclient_mock.get(APP_SERVER + "/v2/things", side_effect=_things)
        assert await cloud.things() == []
        logins = [
            call for call in aioclient_mock.mock_calls if call[1] == URL(LOGIN_URL)
        ]
        assert len(logins) == 2

    async def test_wrong_password(self, session, aioclient_mock: AiohttpClientMocker):
        aioclient_mock.post(LOGIN_URL, json={"error_code": -20601, "msg": "wrong"})
        client = TapoCloudClient(session, "user@example.com", "bad")
        with pytest.raises(TapoCloudAuthError):
            await client.things()

    async def test_ssl_context_trusts_tplink_root(self, cloud: TapoCloudClient):
        context = await cloud._get_ssl_context()
        common_names = {
            value
            for ca in context.get_ca_certs()
            for rdn in ca["subject"]
            for key, value in rdn
            if key == "commonName"
        }
        assert "TP-Link Cloud Root CA" in common_names


async def test_bridge_answers_trigger_logs_from_cloud():
    hub_protocol = FakeSmartCamProtocol()
    cloud_client = FakeTapoCloudClient(hub_protocol)
    bridge = SmartCamBridgeProtocol(
        hub_protocol, "192.168.1.61", TapoCloudEventLog(cloud_client)
    )
    hub = TapoHub("192.168.1.61", 443, TapoClient(None, "https://x", bridge))
    await hub.update()
    hub_protocol.push_event("rotation", rotate_deg=60)

    button = next(c for c in hub.children if c.device_id == BUTTON_ID)
    logs = (await button.get_event_logs(page_size=5)).get_or_raise()

    assert logs.events[0].degrees == 60
    assert cloud_client.event_requests == [BUTTON_ID]
    assert all(
        r["request_data"]["method"] != "get_trigger_logs"
        for r in hub_protocol.child_requests()
    )
