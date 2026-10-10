"""Read S200B/S200D button events from the Tapo cloud Event Log.

The H200 hub does not keep its buttons' trigger logs locally (``get_trigger_logs``
always returns an empty list), but it uploads every click and dial rotation to the
Tapo cloud, where the Tapo app shows them as the device "Event Log".

This is an unofficial, undocumented API used by the Tapo Android app, so endpoints
and payloads may change without notice. The request flow follows
https://github.com/FlyzDev/tapo-s200b-h200-homeassistant (MIT) and the app
signing constants documented by https://github.com/dimme/tapo-cli.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path
import ssl
import time
from typing import Any, Optional
from urllib.parse import quote
import uuid

import aiohttp
import certifi

_LOGGER = logging.getLogger(__name__)

LOGIN_URLS = ("https://eu-wap.tplinkcloud.com", "https://wap.tplinkcloud.com")
APP_SERVER_SERVICE_ID = "nbu.iot-app-server.app-v2"
# Public Tapo Android app constants, not per-user secrets
ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
SIGNING_KEY = "6ed7d97f3e73467f8a5bab90b577ba4c"
APP_TYPE = "Tapo_Android"
APP_VERSION = "3.21.112"
USER_AGENT = "okhttp/3.12.13"

# App servers (*.iot.i.tplinkcloud.com) present certificates issued by TP-Link's
# private "TP-Link Cloud Root CA" (sha256 8D:27:22:4D:AC:20:C2:DB:8A:38:0E:31:03:B4:
# E3:83:C4:81:EE:41:B6:81:90:85:79:AE:E4:16:76:1D:A2:89); login hosts use public CAs.
ROOT_CA_FILE = Path(__file__).with_name("tplink_cloud_root_ca.pem")

SUPPORTED_EVENTS = {"singleClick", "doubleClick", "rotation"}
# Buttons behind an H200 report a double press as two singleClick entries; the
# cloud timestamps have one second resolution.
DOUBLE_CLICK_WINDOW_S = 1
_AS_IS, _DOUBLE, _ABSORBED = "as_is", "double", "absorbed"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class TapoCloudError(Exception):
    """Error talking to the Tapo cloud."""


class TapoCloudAuthError(TapoCloudError):
    """The Tapo cloud rejected the account credentials."""


class _TokenExpired(TapoCloudError):
    pass


def build_ssl_context() -> ssl.SSLContext:
    """Trust public roots plus TP-Link's cloud root (blocking, run in executor)."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(ROOT_CA_FILE))
    return context


class TapoCloudClient:
    """Minimal async client for the Tapo Thing cloud API."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._terminal_uuid = uuid.uuid4().hex.upper()
        self._ssl_context: Optional[ssl.SSLContext] = None
        self._token: Optional[str] = None
        self._app_server_url: Optional[str] = None

    async def things(self) -> list[dict[str, Any]]:
        """Return every device ("thing") bound to the account."""
        data = await self._get(
            "/v2/things",
            {
                "page": 0,
                "pageSize": 200,
                "includeKasaShareDevices": "false",
                "includePcDevice": "false",
                "includeMatterDevice": "false",
                "includeExternalVendorDeviceInfo": "false",
            },
        )
        return [thing for thing in data.get("data") or [] if isinstance(thing, dict)]

    async def events(
        self, thing: dict[str, Any], fetch_size: int
    ) -> list[dict[str, Any]]:
        """Return the newest Event Log entries of a thing, newest first."""
        path = f"/v1/things/{quote(str(thing['thingName']), safe='')}/events"
        base_url = thing.get("appServerUrlV2") or thing.get("appServerUrl")
        data = await self._get(
            path,
            {"fetchSize": fetch_size, "order": "DESC", "fetchType": "normal"},
            base_url=base_url,
        )
        return [event for event in data.get("events") or [] if isinstance(event, dict)]

    async def _get(
        self, path: str, params: dict[str, Any], base_url: Optional[str] = None
    ) -> dict[str, Any]:
        if self._token is None:
            await self._login()
        try:
            return await self._get_once(path, params, base_url)
        except _TokenExpired:
            _LOGGER.debug("Tapo cloud token expired, logging in again")
            await self._login()
            return await self._get_once(path, params, base_url)

    async def _get_once(
        self, path: str, params: dict[str, Any], base_url: Optional[str]
    ) -> dict[str, Any]:
        assert self._token is not None and self._app_server_url is not None
        async with self._session.get(
            (base_url or self._app_server_url) + path,
            params=params,
            headers=self._thing_headers(self._token),
            ssl=await self._get_ssl_context(),
            timeout=REQUEST_TIMEOUT,
        ) as response:
            text = await response.text()
            status = response.status
        if status in (401, 403):
            self._token = None
            raise _TokenExpired(f"HTTP {status}")
        if status >= 400:
            raise TapoCloudError(f"Tapo cloud request {path} failed: HTTP {status}")
        data = _parse(text)
        if code := _error_code(data):
            raise TapoCloudError(f"Tapo cloud request {path} failed: error {code}")
        return data

    async def _login(self) -> None:
        failures = []
        for base_url in LOGIN_URLS:
            try:
                token = await self._login_token(base_url)
                app_server_url = await self._app_server(base_url)
            except TapoCloudAuthError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TapoCloudError) as error:
                failures.append(f"{base_url}: {error}")
                continue
            self._token, self._app_server_url = token, app_server_url
            _LOGGER.debug("Logged in to Tapo cloud, app server %s", app_server_url)
            return
        raise TapoCloudError("Tapo cloud login failed: " + "; ".join(failures))

    async def _login_token(self, base_url: str) -> str:
        payload = {
            "method": "login",
            "params": {
                "appType": APP_TYPE,
                "cloudUserName": self._username,
                "cloudPassword": self._password,
                "terminalUUID": self._terminal_uuid,
            },
        }
        data = await self._post(base_url, payload, headers={})
        if code := _error_code(data):
            # -20600 account not found, -20601 wrong password, -20675 locked
            if code in (-20600, -20601, -20675):
                raise TapoCloudAuthError(f"Tapo cloud login rejected: error {code}")
            raise TapoCloudError(f"Tapo cloud login failed: error {code}")
        token = (data.get("result") or {}).get("token")
        if not token:
            raise TapoCloudError("Tapo cloud login returned no token")
        return str(token)

    async def _app_server(self, base_url: str) -> str:
        endpoint = "/api/v2/common/getAppServiceUrlByCloudUserName"
        payload = {
            "cloudUserName": self._username,
            "serviceIds": [APP_SERVER_SERVICE_ID],
        }
        body = json.dumps(payload, separators=(",", ":"))
        data = await self._post(
            base_url + endpoint, body, headers=_signed_headers(body, endpoint)
        )
        if code := _error_code(data):
            raise TapoCloudError(f"Tapo cloud service lookup failed: error {code}")
        for service in (data.get("result") or {}).get("serviceList") or []:
            if service.get("serviceId") == APP_SERVER_SERVICE_ID:
                return str(service["serviceUrl"])
        raise TapoCloudError("Tapo cloud app server not found")

    async def _post(
        self, url: str, payload: dict | str, headers: dict[str, str]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = (
            {"data": payload} if isinstance(payload, str) else {"json": payload}
        )
        async with self._session.post(
            url,
            headers={"User-Agent": USER_AGENT, **headers},
            ssl=await self._get_ssl_context(),
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise TapoCloudError(f"HTTP {response.status}")
        return _parse(text)

    async def _get_ssl_context(self) -> ssl.SSLContext:
        if (context := self._ssl_context) is None:
            context = await asyncio.get_running_loop().run_in_executor(
                None, build_ssl_context
            )
            self._ssl_context = context
        return context

    def _thing_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": "ut|" + token,
            "app-cid": "app:Tapo_Android:" + self._terminal_uuid,
            "x-app-name": APP_TYPE,
            "x-app-version": APP_VERSION,
            "x-term-id": self._terminal_uuid,
            "x-app-ospf": "Android",
            "x-app-brand": "TPLINK",
            "User-Agent": USER_AGENT,
        }


class TapoCloudEventLog:
    """Serve hub children trigger logs from the Tapo cloud Event Log.

    Answers in the same shape as the local ``get_trigger_logs`` request, so the
    existing plugp100 button devices and Home Assistant event entities work
    unchanged. Each button hits the cloud at most once per
    ``MIN_FETCH_INTERVAL_S``, and failures back off so the account is not hammered.
    """

    MIN_FETCH_INTERVAL_S = 1.0
    LOGIN_BACKOFF_S = (30, 60, 120, 300, 600)

    def __init__(self, client: TapoCloudClient) -> None:
        self._client = client
        self._things: dict[str, dict[str, Any]] = {}
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._click_decisions: dict[str, dict[int, str]] = {}
        self._lock = asyncio.Lock()
        self._failures = 0
        self._retry_at = 0.0

    async def get_trigger_logs(
        self, device_id: str, mac: Optional[str], page_size: int
    ) -> dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(device_id)
            if cached and now - cached[0] < self.MIN_FETCH_INTERVAL_S:
                return cached[1]
            if now < self._retry_at:
                raise TapoCloudError(
                    f"Tapo cloud unavailable, retrying in {self._retry_at - now:.0f}s"
                )
            try:
                thing = await self._find_thing(device_id, mac)
                events = await self._client.events(thing, max(page_size, 20))
            except TapoCloudError:
                self._retry_at = (
                    time.monotonic()
                    + self.LOGIN_BACKOFF_S[
                        min(self._failures, len(self.LOGIN_BACKOFF_S) - 1)
                    ]
                )
                self._failures += 1
                raise
            self._failures = 0
            logs = to_trigger_logs(events)
            logs["logs"] = merge_double_clicks(
                logs["logs"], self._click_decisions.setdefault(device_id, {})
            )
            logs["sum"] = len(logs["logs"])
            self._cache[device_id] = (time.monotonic(), logs)
            return logs

    async def _find_thing(self, device_id: str, mac: Optional[str]) -> dict[str, Any]:
        if device_id not in self._things:
            things = await self._client.things()
            by_mac = {_normalize_mac(t.get("mac")): t for t in things if t.get("mac")}
            for thing in things:
                self._things[str(thing.get("thingName"))] = thing
            if device_id not in self._things and _normalize_mac(mac) in by_mac:
                self._things[device_id] = by_mac[_normalize_mac(mac)]
        if (thing := self._things.get(device_id)) is None:
            raise TapoCloudError(f"Device {device_id} not found in the Tapo cloud")
        return thing


def to_trigger_logs(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert cloud Event Log entries to the local ``get_trigger_logs`` format."""
    logs = []
    for event in events:
        name = event.get("name")
        params = event.get("params")
        if not isinstance(params, dict):
            params = {}
        if name not in SUPPORTED_EVENTS or (
            name == "rotation" and "rotate_deg" not in params
        ):
            _LOGGER.debug("Ignoring unsupported Tapo cloud event %s", name)
            continue
        try:
            log_id = int(event["logId"])
        except (KeyError, TypeError, ValueError):
            continue
        logs.append(
            {
                "id": log_id,
                "timestamp": event.get("timestamp"),
                "event": name,
                "params": params,
            }
        )
    logs.sort(key=lambda log: log["id"], reverse=True)
    return {"start_id": logs[0]["id"] if logs else 0, "sum": len(logs), "logs": logs}


def merge_double_clicks(
    logs: list[dict[str, Any]], decisions: dict[int, str]
) -> list[dict[str, Any]]:
    """Merge two single clicks at most ``DOUBLE_CLICK_WINDOW_S`` apart into a doubleClick.

    ``logs`` are newest first; the merged entry keeps the id of the second click.
    ``decisions`` remembers, per log id, what was returned before (the event as
    is, ``DOUBLE`` or ``ABSORBED``) and is updated in place. Earlier decisions are
    replayed unchanged and only clicks never returned before can pair up, so the
    ids and types the event entities have seen stay stable between fetches.
    """
    merged: list[dict[str, Any]] = []
    pending: Optional[dict[str, Any]] = None
    for log in reversed(logs):
        decision = decisions.get(log["id"])
        if decision == _ABSORBED:
            continue
        if decision is not None:
            merged.append(
                {**log, "event": "doubleClick"} if decision == _DOUBLE else log
            )
            pending = None
            continue
        if (
            pending is not None
            and log["event"] == "singleClick"
            and _seconds_apart(pending, log) <= DOUBLE_CLICK_WINDOW_S
        ):
            decisions[pending["id"]] = _ABSORBED
            decisions[log["id"]] = _DOUBLE
            merged[-1] = {**log, "event": "doubleClick"}
            pending = None
            continue
        decisions[log["id"]] = _AS_IS
        merged.append(log)
        pending = log if log["event"] == "singleClick" else None
    # forget decisions about entries that dropped out of the fetched page
    for log_id in decisions.keys() - {log["id"] for log in logs}:
        del decisions[log_id]
    merged.reverse()
    return merged


def _seconds_apart(first: dict[str, Any], second: dict[str, Any]) -> float:
    try:
        return abs(float(second["timestamp"]) - float(first["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _signed_headers(body: str, endpoint: str) -> dict[str, str]:
    timestamp, nonce = str(int(time.time())), str(uuid.uuid4())
    content_md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode()
    signature = hmac.new(
        SIGNING_KEY.encode(),
        f"{content_md5}\n{timestamp}\n{nonce}\n{endpoint}".encode(),
        hashlib.sha1,
    ).hexdigest()
    return {
        "Content-Md5": content_md5,
        "X-Authorization": f"Timestamp={timestamp}, Nonce={nonce}, "
        f"AccessKey={ACCESS_KEY}, Signature={signature}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _parse(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise TapoCloudError("Invalid Tapo cloud response") from error
    return data if isinstance(data, dict) else {}


def _error_code(data: dict[str, Any]) -> int:
    for key in ("error_code", "errorCode"):
        if key in data:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                return -1
    return 0


def _normalize_mac(mac: Optional[str]) -> str:
    return "".join(c for c in str(mac or "") if c.isalnum()).upper()
