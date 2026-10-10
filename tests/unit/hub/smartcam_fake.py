"""In-memory H200 hub answering like python-kasa's SmartCamProtocol.query."""

import base64
from typing import Any

HUB_DEVICE_ID = "H200_DEVICE_ID"
HUB_MAC = "7C-F1-7E-00-00-01"
BUTTON_ID = "S200B_DEVICE_ID"
DOOR_ID = "T110_DEVICE_ID"
CAMERA_ID = "D230_DEVICE_ID"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _sensor_child(device_id: str, model: str, nickname: str, **extra) -> dict:
    return {
        "at_low_battery": False,
        "category": "subg.trigger.button",
        "device_id": device_id,
        "fw_ver": "1.12.0 Build 231121 Rel.092508",
        "hw_id": "00000000000000000000000000000000",
        "hw_ver": "1.0",
        "mac": "3C52A1000000",
        "model": model,
        "nickname": _b64(nickname),
        "oem_id": "00000000000000000000000000000000",
        "parent_device_id": HUB_DEVICE_ID,
        "region": "Europe/Budapest",
        "report_interval": 16,
        "rssi": -45,
        "signal_level": 3,
        "status": "online",
        "type": "SMART.TAPOSENSOR",
        **extra,
    }


def _components(*ids: str) -> dict:
    return {"component_list": [{"id": cid, "ver_code": 1} for cid in ids]}


class FakeSmartCamProtocol:
    """Fake python-kasa SmartCamProtocol for an H200 with an S200B, a T110 and a camera."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.trigger_logs: list[dict[str, Any]] = []
        self.cloud_events: dict[str, list[dict[str, Any]]] = {}
        self.child_error_code: int = 0
        self.children = {
            BUTTON_ID: _sensor_child(BUTTON_ID, "S200B", "Desk button"),
            DOOR_ID: _sensor_child(
                DOOR_ID,
                "T110",
                "Front door",
                category="subg.trigger.contact-sensor",
                open=True,
            ),
        }
        self.child_components = {
            BUTTON_ID: _components(
                "device", "trigger_log", "battery_detect", "double_click", "firmware"
            ),
            DOOR_ID: _components("device", "trigger_log", "battery_detect"),
        }
        self.camera = {
            "alias": "Doorbell",
            "category": "camera",
            "device_id": CAMERA_ID,
            "device_model": "D230",
            "device_type": "SMART.TAPODOORBELL",
            "sw_ver": "1.1.19 Build 20241011 rel.67020",
        }

    async def query(self, request: dict, retry_count: int = 3) -> dict:
        self.requests.append(request)
        method, params = next(iter(request.items()))
        if method == "getDeviceInfo":
            return {method: {"device_info": {"basic_info": self._basic_info()}}}
        if method == "getAppComponentList":
            return {
                method: {
                    "app_component": {
                        "app_component_list": [
                            {"name": "firmware", "version": 2},
                            {"name": "siren", "version": 2},
                            {"name": "childControl", "version": 1},
                        ]
                    }
                }
            }
        if method == "getChildDeviceList":
            children = [self.camera, *self.children.values()]
            return {
                method: {
                    "child_device_list": children,
                    "start_index": 0,
                    "sum": len(children),
                }
            }
        if method == "multipleRequest":
            return {
                method: {
                    "responses": [
                        self._control_child(r["params"]["childControl"])
                        for r in params["requests"]
                    ]
                }
            }
        raise AssertionError(f"Unexpected request {request}")

    async def close(self) -> None:
        self.closed = True

    def child_requests(self, device_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r["params"]["childControl"]
            for request in self.requests
            if "multipleRequest" in request
            for r in request["multipleRequest"]["requests"]
            if device_id is None
            or r["params"]["childControl"]["device_id"] == device_id
        ]

    def _basic_info(self) -> dict:
        return {
            "dev_id": HUB_DEVICE_ID,
            "device_alias": "Living room hub",
            "device_model": "H200",
            "device_type": "SMART.TAPOHUB",
            "hw_id": "00000000000000000000000000000000",
            "hw_version": "1.0",
            "local_ip": "192.168.1.61",
            "mac": HUB_MAC,
            "oem_id": "00000000000000000000000000000000",
            "region": "EU",
            "sw_version": "1.3.6 Build 20240829 rel.71119",
        }

    def _control_child(self, child_control: dict) -> dict:
        device_id = child_control["device_id"]
        child_method = child_control["request_data"]["method"]
        if self.child_error_code:
            data = {"error_code": self.child_error_code}
        elif child_method == "component_nego":
            data = {"error_code": 0, "result": self.child_components[device_id]}
        elif child_method == "get_device_info":
            data = {"error_code": 0, "result": self.children[device_id]}
        elif child_method == "get_trigger_logs":
            logs = list(reversed(self.trigger_logs))
            data = {
                "error_code": 0,
                "result": {
                    "start_id": logs[0]["id"] if logs else 0,
                    "sum": len(logs),
                    "logs": logs[
                        : child_control["request_data"]["params"]["page_size"]
                    ],
                },
            }
        elif child_method == "get_latest_fw":
            data = {"error_code": 0, "result": {"need_to_upgrade": False}}
        else:
            data = {"error_code": -40210}
        return {
            "method": "controlChild",
            "result": {"response_data": data},
            "error_code": 0,
        }

    def push_event(self, event: str, device_id: str = BUTTON_ID, **params) -> None:
        """Record a button action in the local trigger log and the cloud Event Log."""
        log_id = len(self.trigger_logs) + 1
        entry = {"id": log_id, "timestamp": 1700000000, "event": event}
        if params:
            entry["params"] = params
        self.trigger_logs.append(entry)
        self.cloud_events.setdefault(device_id, []).append(
            {
                "logId": log_id,
                "eventId": f"event-{log_id}",
                "name": event,
                "timestamp": 1700000000,
                "params": params,
            }
        )


class FakeTapoCloudClient:
    """Fake TapoCloudClient serving the Event Log recorded by FakeSmartCamProtocol."""

    def __init__(self, hub: FakeSmartCamProtocol) -> None:
        self._hub = hub
        self.event_requests: list[str] = []

    async def things(self) -> list[dict[str, Any]]:
        return [
            {"thingName": device_id, "model": child["model"], "mac": child["mac"]}
            for device_id, child in self._hub.children.items()
        ]

    async def events(self, thing: dict[str, Any], fetch_size: int) -> list[dict]:
        self.event_requests.append(thing["thingName"])
        events = self._hub.cloud_events.get(thing["thingName"], [])
        return list(reversed(events))[:fetch_size]
