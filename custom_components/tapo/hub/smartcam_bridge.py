"""Bridge plugp100 hub devices to hubs that speak the Tapo "smartcam" API.

The Tapo H200 hub does not expose the KLAP/passthrough API used by the H100.
It only listens on HTTPS:443 and speaks the camera style API (SslAes login,
camelCase methods such as ``getDeviceInfo`` and ``controlChild``), which
plugp100 does not implement.

The sensors paired to it (S200B, T110, T31x, ...) still speak the regular
smart protocol, wrapped inside ``controlChild``. This module provides a
plugp100 ``TapoProtocol`` backed by python-kasa's ``SmartCamProtocol`` that
translates the handful of hub-level requests plugp100 needs and tunnels child
requests unchanged, so the hub is exposed as a regular plugp100 ``TapoHub``
and every existing hub platform keeps working.

The H200 does not keep button trigger logs locally, so ``get_trigger_logs``
requests for its children are answered from the Tapo cloud Event Log instead
(see ``tapo_cloud``).
"""

import base64
import dataclasses
import logging
from typing import Any, Optional

import aiohttp
from kasa import (
    AuthenticationError,
    Credentials,
    DeviceConfig,
    DeviceConnectionParameters,
    DeviceEncryptionType,
    DeviceFamily,
)
from kasa.device_factory import get_protocol
from plugp100.api.protocol.tapo_protocol import TapoProtocol
from plugp100.api.requests.tapo_request import TapoRequest
from plugp100.api.tapo_client import TapoClient
from plugp100.api.transport.response import TapoResponse
from plugp100.common.credentials import AuthCredential
from plugp100.common.functional.tri import Failure, Success, Try
from plugp100.devices.hub import TapoHub
from plugp100.discovery import DiscoveredDevice
from plugp100.errors import InvalidAuthentication, TapoException
from plugp100.models.hub_base import HubChildBaseInfo

from custom_components.tapo.hub.tapo_cloud import TapoCloudClient, TapoCloudEventLog

_LOGGER = logging.getLogger(__name__)

SMARTCAM_HUB_PORT = 443
SMARTCAM_HUB_DEVICE_TYPE = "SMART.TAPOHUB"

# smartcam component names that plugp100 knows under a different id
_COMPONENT_ID_MAPPING = {"childControl": "control_child"}


def is_smartcam_hub(discovered_device: Optional[DiscoveredDevice]) -> bool:
    """Return true when discovery reports a hub that only speaks HTTPS (e.g. H200)."""
    if discovered_device is None or discovered_device.device_type is None:
        return False
    encrypt_schema = discovered_device.mgt_encrypt_schm
    return (
        discovered_device.device_type.upper() == SMARTCAM_HUB_DEVICE_TYPE
        and encrypt_schema is not None
        and bool(encrypt_schema.is_support_https)
    )


async def connect_smartcam_hub(
    host: str,
    credentials: AuthCredential,
    session: Optional[aiohttp.ClientSession] = None,
    port: Optional[int] = None,
) -> TapoHub:
    """Create a plugp100 TapoHub that talks to a smartcam hub through python-kasa."""
    config = DeviceConfig(
        host=host,
        port_override=port,
        credentials=Credentials(credentials.username, credentials.password),
        connection_type=DeviceConnectionParameters(
            device_family=DeviceFamily.SmartTapoHub,
            encryption_type=DeviceEncryptionType.Aes,
            https=True,
        ),
        http_client=session,
    )
    cloud_event_log = (
        TapoCloudEventLog(
            TapoCloudClient(session, credentials.username, credentials.password)
        )
        if session
        else None
    )
    protocol = SmartCamBridgeProtocol(get_protocol(config), host, cloud_event_log)
    url = f"https://{host}:{port or SMARTCAM_HUB_PORT}"
    client = TapoClient(credentials, url, protocol, session)
    return TapoHub(host, port or SMARTCAM_HUB_PORT, client)


class SmartCamBridgeProtocol(TapoProtocol):
    """plugp100 protocol that forwards requests to a python-kasa SmartCamProtocol."""

    def __init__(
        self,
        kasa_protocol,
        host: str,
        cloud_event_log: Optional[TapoCloudEventLog] = None,
    ) -> None:
        self._protocol = kasa_protocol
        self._host = host
        self._cloud_event_log = cloud_event_log
        self._child_macs: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "SmartCam"

    async def send_request(
        self, request: TapoRequest, retry: int = 3
    ) -> Try[TapoResponse[dict[str, Any]]]:
        try:
            return Success(TapoResponse(0, await self._dispatch(request, retry), None))
        except AuthenticationError as error:
            _LOGGER.debug("Authentication failed for %s: %s", self._host, error)
            return Failure(InvalidAuthentication(self._host, SMARTCAM_HUB_DEVICE_TYPE))
        except Exception as error:  # pylint: disable=broad-except
            return Failure(error)

    async def close(self):
        await self._protocol.close()

    async def _dispatch(self, request: TapoRequest, retry: int):
        method = request.method
        if method == "control_child":
            return await self._control_child(request.params, retry)
        if method == "get_device_info":
            return await self._get_device_info(retry)
        if method == "component_nego":
            return await self._get_components(retry)
        if method == "get_child_device_list":
            return await self._get_child_device_list(request.params, retry)
        raise TapoException(
            None, f"Request {method} is not supported by smartcam hub bridge"
        )

    async def _query(self, method: str, params: dict[str, Any], retry: int):
        response = await self._protocol.query({method: params}, retry)
        return response[method]

    async def _get_device_info(self, retry: int) -> dict[str, Any]:
        response = await self._query(
            "getDeviceInfo", {"device_info": {"name": ["basic_info", "info"]}}, retry
        )
        return smartcam_to_smart_device_info(response["device_info"]["basic_info"])

    async def _get_components(self, retry: int) -> dict[str, Any]:
        response = await self._query(
            "getAppComponentList",
            {"app_component": {"name": "app_component_list"}},
            retry,
        )
        return smartcam_to_smart_components(
            response["app_component"]["app_component_list"]
        )

    async def _get_child_device_list(self, params, retry: int) -> dict[str, Any]:
        start_index = getattr(params, "start_index", 0) or 0
        # python-kasa already follows the pagination and returns every child
        response = await self._query(
            "getChildDeviceList", {"childControl": {"start_index": 0}}, retry
        )
        children = filter_smart_children(response.get("child_device_list", []))
        self._child_macs = {child["device_id"]: child["mac"] for child in children}
        return {
            "child_device_list": children[start_index:],
            "start_index": start_index,
            "sum": len(children),
        }

    async def _control_child(self, params, retry: int):
        device_id = params.device_id
        # plugp100 always wraps child requests in a multipleRequest
        child_requests: list[TapoRequest] = list(params.requestData.params.requests)
        if self._cloud_event_log is not None and [r.method for r in child_requests] == [
            "get_trigger_logs"
        ]:
            logs = await self._cloud_event_log.get_trigger_logs(
                device_id,
                self._child_macs.get(device_id),
                getattr(child_requests[0].params, "page_size", 5),
            )
            return _child_responses([("get_trigger_logs", logs)])

        requests = [
            {
                "method": "controlChild",
                "params": {
                    "childControl": {
                        "device_id": device_id,
                        "request_data": {
                            "method": child_request.method,
                            "params": _encode_params(child_request.params),
                        },
                    }
                },
            }
            for child_request in child_requests
        ]
        response = await self._query("multipleRequest", {"requests": requests}, retry)

        results = []
        for child_request, child_response in zip(
            child_requests, response.get("responses", [])
        ):
            error_code = child_response.get("error_code", 0)
            response_data = (child_response.get("result") or {}).get(
                "response_data", {}
            )
            error_code = error_code or response_data.get("error_code", 0)
            if error_code != 0:
                raise TapoException.from_error_code(error_code, None)
            results.append((child_request.method, response_data.get("result", {})))
        return _child_responses(results)


def _child_responses(results: list[tuple[str, Any]]) -> dict[str, Any]:
    """Wrap child results the way plugp100's ``TapoClient.control_child`` expects."""
    responses = [
        {"method": method, "result": result, "error_code": 0}
        for method, result in results
    ]
    return {"responseData": {"result": {"responses": responses}}}


def smartcam_to_smart_device_info(basic_info: dict[str, Any]) -> dict[str, Any]:
    """Convert a smartcam ``basic_info`` payload to plugp100's device info format."""
    alias = basic_info.get("device_alias") or basic_info.get("device_name") or ""
    return {
        "device_id": basic_info["dev_id"],
        "hw_id": basic_info.get("hw_id", ""),
        "oem_id": basic_info.get("oem_id", ""),
        "fw_ver": basic_info["sw_version"],
        "hw_ver": basic_info["hw_version"],
        "mac": basic_info["mac"],
        "nickname": base64.b64encode(alias.encode("UTF-8")).decode(),
        "model": basic_info["device_model"],
        "type": basic_info["device_type"],
        "ip": basic_info.get("local_ip"),
        "has_set_location_info": bool(basic_info.get("has_set_location_info")),
        "latitude": basic_info.get("latitude"),
        "longitude": basic_info.get("longitude"),
        "region": basic_info.get("region"),
    }


def smartcam_to_smart_components(
    app_component_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert a smartcam ``app_component_list`` to plugp100's component format."""
    return {
        "component_list": [
            {
                "id": _COMPONENT_ID_MAPPING.get(component["name"], component["name"]),
                "ver_code": component["version"],
            }
            for component in app_component_list
        ]
    }


def filter_smart_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only children that speak the smart protocol (sensors, buttons, ...).

    Cameras paired to the hub use a different payload and API, so they are skipped.
    """
    supported = []
    for child in children:
        if HubChildBaseInfo.from_json(child).is_success():
            supported.append(child)
        else:
            _LOGGER.debug(
                "Skipping hub child %s (%s): not a smart protocol device",
                child.get("device_id"),
                child.get("device_model") or child.get("model"),
            )
    return supported


def _encode_params(params) -> Any:
    if params is None or isinstance(params, dict):
        return params
    if dataclasses.is_dataclass(params) and not isinstance(params, type):
        return {
            key: value
            for key, value in dataclasses.asdict(params).items()
            if value is not None
        }
    return params
