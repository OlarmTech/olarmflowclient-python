"""Olarm API - The official async Python client for interacting with the Olarm HTTP API.

See https://www.olarm.com for more info.
"""

import asyncio
from collections.abc import Callable
import json
import logging
import ssl
from typing import Any, Literal
import urllib.parse

import aiohttp
import aiomqtt

from .const import (
    BASE_URL,
    MQTT_HOST,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    MQTT_USER,
    MQTT_RETRIES_BEFORE_DISCONNECT,
    MQTT_RECONNECT_BACKOFF_MAX,
    MQTT_RECONNECT_BACKOFF_MIN,
)

_LOGGER = logging.getLogger(__name__)


class OlarmFlowClientApiError(Exception):
    """Raised when the API returns an error."""

    # Standard HTTP error descriptions
    HTTP_ERROR_DESCRIPTIONS = {
        400: "Bad request",
        401: "Access token expired",
        403: "Unauthorized",
        404: "Not found",
        429: "Request was rate limited",
        500: "Olarm server error",
        502: "Olarm service temporarily unavailable",
        503: "Olarm service temporarily unavailable",
        504: "Gateway timeout reaching the Olarm service - network issue or service under load",
    }

    # Descriptions for machine-readable error codes returned by the Olarm API.
    # Used as a fallback when the server response body lacks a message
    # (e.g. older deployed API versions).
    ERROR_CODE_DESCRIPTIONS = {
        "tokenExpired": "Your access token has expired. Please refresh the token or sign in again.",
        "tokenInvalid": "The access token is invalid or malformed. Please sign in again.",
        "sessionFailed": "Your session could not be loaded or has been revoked. Please sign in again.",
        "rateLimited": "Too many requests. Please wait a few seconds and try again.",
        "originNotAllowed": "The request origin is not allowed. This is often caused by a proxy or firewall rewriting request headers.",
        "hostNotAllowed": "The request host is not allowed. This is often caused by a proxy or firewall rewriting request headers.",
        "insufficientScope": "This token does not have permission to access this endpoint.",
        "apiAccessNotAllowed": "This endpoint is not available via the public API.",
        "authFailed": "Authentication failed. Please sign in again.",
    }

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_text: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        req_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        """Initialize the API error."""
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
        self.error_code = error_code
        self.error_message = error_message
        self.req_id = req_id
        self.retry_after = retry_after

    def __str__(self) -> str:
        """Return string representation of the error.

        The raw response body is deliberately left out: gateways return HTML
        error pages and these strings are surfaced directly to end users.
        Use `response_text` when the body is needed for diagnostics.
        """
        if not self.status_code:
            return super().__str__()

        message = super().__str__()

        # Prefer the specific error code over the generic HTTP description
        error_desc = self.HTTP_ERROR_DESCRIPTIONS.get(self.status_code, "")
        label = f"API Error {self.status_code}"
        if self.error_code:
            label += f" ({self.error_code})"
        elif error_desc and error_desc != message:
            label += f" ({error_desc})"

        # Prefer the server-supplied message, then the local code description
        detail = self.error_message or self.ERROR_CODE_DESCRIPTIONS.get(
            self.error_code or "", ""
        )
        result = f"{label}: {message}"
        if detail and detail != message:
            result += f" - {detail}"
        if self.req_id:
            result += f" [reqId={self.req_id}]"
        return result


class TokenExpired(OlarmFlowClientApiError):
    """Raised when the access token has expired (401)."""

    def __init__(
        self, message: str = "Access token has expired", **kwargs: Any
    ) -> None:
        """Initialize the token expired error."""
        kwargs.setdefault("status_code", 401)
        super().__init__(message, **kwargs)


class Unauthorized(OlarmFlowClientApiError):
    """Raised when the request is unauthorized (403)."""

    def __init__(self, message: str = "Unauthorized access", **kwargs: Any) -> None:
        """Initialize the unauthorized error."""
        kwargs.setdefault("status_code", 403)
        super().__init__(message, **kwargs)


class DeviceNotFound(OlarmFlowClientApiError):
    """Raised when a specific device is not found or not accessible (404, 403)."""

    def __init__(self, device_id: str | None = None, **kwargs: Any) -> None:
        """Initialize the device not found error."""
        message = f"Device '{device_id}' not found" if device_id else "Device not found"
        kwargs.setdefault("status_code", 404)
        super().__init__(message, **kwargs)


class DevicesNotFound(OlarmFlowClientApiError):
    """Raised when no devices are found for the account (404)."""

    def __init__(
        self, message: str = "No devices found for this account", **kwargs: Any
    ) -> None:
        """Initialize the devices not found error."""
        kwargs.setdefault("status_code", 404)
        super().__init__(message, **kwargs)


class ServerError(OlarmFlowClientApiError):
    """Raised when the server returns an internal error (500)."""

    def __init__(self, message: str = "Server internal error", **kwargs: Any) -> None:
        """Initialize the server error."""
        kwargs.setdefault("status_code", 500)
        super().__init__(message, **kwargs)


class ServiceUnavailable(OlarmFlowClientApiError):
    """Raised when the Olarm service is unreachable via the gateway (502, 503, 504)."""

    def __init__(
        self,
        message: str = "Olarm service temporarily unavailable",
        **kwargs: Any,
    ) -> None:
        """Initialize the service unavailable error."""
        kwargs.setdefault("status_code", 503)
        super().__init__(message, **kwargs)


class RateLimited(OlarmFlowClientApiError):
    """Raised when the request is rate limited (429)."""

    def __init__(
        self, message: str = "Too many requests - rate limited", **kwargs: Any
    ) -> None:
        """Initialize the rate limited error."""
        kwargs.setdefault("status_code", 429)
        super().__init__(message, **kwargs)


class OlarmFlowClientConnectionError(OlarmFlowClientApiError):
    """Raised when the Olarm API cannot be reached at all.

    Covers client-side network failures: DNS resolution, TLS issues,
    firewalls blocking outbound HTTPS, connection resets and timeouts.
    """

    def __init__(
        self, message: str = "Unable to connect to the Olarm API", **kwargs: Any
    ) -> None:
        """Initialize the connection error."""
        super().__init__(message, **kwargs)


class MqttConnectError(OlarmFlowClientApiError):
    """Raised when MQTT connection fails."""

    def __init__(self, message: str = "MQTT connection failed") -> None:
        """Initialize the MQTT connection error."""
        super().__init__(message)


class MqttAuthError(MqttConnectError):
    """Raised when the MQTT broker refuses the connection due to failed authentication.

    Subclass of MqttConnectError so existing handlers keep working; callers
    may catch it separately to trigger reauthentication flows.
    """

    def __init__(self, message: str = "MQTT authentication failed") -> None:
        """Initialize the MQTT authentication error."""
        super().__init__(message)


class MqttTimeoutError(OlarmFlowClientApiError):
    """Raised when MQTT connection times out."""

    def __init__(self, message: str = "MQTT connection timeout") -> None:
        """Initialize the MQTT timeout error."""
        super().__init__(message)


class OlarmFlowClient:
    """Async client class for interacting with the Olarm API."""

    def __init__(
        self,
        access_token: str,
        expires_at: float | None = None,
        mqtt_retries_before_disconnect: int = MQTT_RETRIES_BEFORE_DISCONNECT,
    ) -> None:
        """Initialize the Olarm Flow Client."""

        # tokens
        self._access_token = access_token
        self._expires_at = expires_at
        self._is_jwt_token = (
            len(self._access_token.split(".")) == 3 and self._expires_at is not None
        )

        # api client attributes (initialized to None)
        self._api_session: aiohttp.ClientSession | None = None

        # mqtt client attributes (initialized to None)
        self._mqtt_clientId: str | None = None
        self._mqtt_client: aiomqtt.Client | None = None
        self._mqtt_task: asyncio.Task[None] | None = None
        # Strong refs to fire-and-forget tasks so they aren't GC'd early
        self._mqtt_bg_tasks: set[asyncio.Task[None]] = set()
        self._mqtt_callbacks: dict[str, Callable[[str, dict[str, Any]], None]] = {}
        self._mqtt_status_callback: (
            Callable[
                [
                    Literal["connecting", "connected", "disconnected", "reconnecting"],
                    dict[str, Any],
                ],
                None,
            ]
            | None
        ) = None
        self._mqtt_retries: int = 0
        self._mqtt_retries_before_disconnect: int = mqtt_retries_before_disconnect
        self._event_loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> "OlarmFlowClient":
        """Async context manager enter."""
        await self._api_connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self._api_close()

    async def _api_connect(self) -> None:
        """Create aiohttp session."""
        if self._api_session is None:
            self._api_session = aiohttp.ClientSession()

    async def _api_close(self) -> None:
        """Close aiohttp session."""
        if self._api_session:
            await self._api_session.close()
            self._api_session = None

    async def _api_make_request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        jsonBody: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an authenticated request to the API."""

        await self._api_connect()
        assert self._api_session is not None  # Guaranteed by _api_connect

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        url = f"{BASE_URL}{endpoint}"
        if params:
            filtered_params = {k: v for k, v in params.items() if v is not None}
            if filtered_params:
                url += "?" + urllib.parse.urlencode(filtered_params)

        kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
        if jsonBody is not None:
            kwargs["json"] = jsonBody

        _LOGGER.debug("API: request %s %s", method, endpoint)

        result: dict[str, Any] = {}
        try:
            async with self._api_session.request(method, url, **kwargs) as response:
                if response.status != 200:
                    text = await response.text()

                    # Extract error detail from the response body (tolerate
                    # non-JSON bodies, e.g. HTML from the gateway on 502/504)
                    error_code: str | None = None
                    error_message: str | None = None
                    req_id: str | None = None
                    try:
                        body = json.loads(text)
                        if isinstance(body, dict):
                            error_code = body.get("error") or (
                                body["errors"][0]
                                if isinstance(body.get("errors"), list)
                                and body["errors"]
                                else None
                            )
                            error_message = body.get("message")
                            req_id = body.get("reqId")
                    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                        pass

                    # Headers take precedence as they survive body rewrites
                    error_code = response.headers.get("X-Olarm-Auth-Error", error_code)
                    req_id = response.headers.get("X-Olarm-Req-Id", req_id)
                    retry_after: int | None = None
                    retry_after_header = response.headers.get("Retry-After")
                    if retry_after_header is not None:
                        try:
                            retry_after = int(retry_after_header)
                        except ValueError:
                            retry_after = None

                    _LOGGER.debug("API: request failed %s %s (status=%s): %s", method, endpoint, response.status, text)

                    raise OlarmFlowClientApiError(
                        "Request failed",
                        status_code=response.status,
                        response_text=text,
                        error_code=error_code,
                        error_message=error_message,
                        req_id=req_id,
                        retry_after=retry_after,
                    )

                if "application/json" in response.headers.get("Content-Type", ""):
                    result = await response.json()
                else:
                    result = await response.text()

        except aiohttp.ClientError as e:
            _LOGGER.debug("API: connection error %s %s: %s", method, endpoint, e)
            raise OlarmFlowClientConnectionError(
                f"Unable to connect to the Olarm API: {e!s}"
            ) from e
        finally:
            await self._api_close()

        return result

    async def _api_send_action(
        self,
        device_id: str,
        action_cmd: str,
        action_num: int,
        prolink_id: str | None = None,
    ) -> dict[str, Any]:
        """Send an action command to a device or prolink."""
        if prolink_id is not None:
            return await self._api_make_request(
                "POST",
                f"/api/v4/prolinks/{prolink_id}/actions",
                jsonBody={"actionCmd": action_cmd, "actionNum": action_num},
            )

        return await self._api_make_request(
            "POST",
            f"/api/v4/devices/{device_id}/actions",
            jsonBody={"actionCmd": action_cmd, "actionNum": action_num},
        )

    def _handle_api_error(self, err: OlarmFlowClientApiError) -> None:
        """Handle common API errors by raising specific exceptions."""
        # Preserve the original error detail on the specific exception
        detail: dict[str, Any] = {
            "status_code": err.status_code,
            "response_text": err.response_text,
            "error_code": err.error_code,
            "error_message": err.error_message,
            "req_id": err.req_id,
            "retry_after": err.retry_after,
        }
        if err.status_code == 401 or err.error_code == "tokenExpired":
            # Older deployed API versions report expired tokens as 403 with
            # an error code, so match on the code as well
            raise TokenExpired(**detail) from err
        elif err.status_code == 403:
            raise Unauthorized(**detail) from err
        elif err.status_code == 429:
            raise RateLimited(**detail) from err
        elif err.status_code == 500:
            raise ServerError(**detail) from err
        elif err.status_code in (502, 503, 504):
            raise ServiceUnavailable(**detail) from err
        else:
            # Re-raise original error for other status codes
            raise err

    async def update_access_token(self, access_token: str, expires_at: float) -> None:
        """Update the access token.

        The MQTT reconnect loop reads the stored token immediately before every
        connect attempt, so the new token is used automatically on the next
        (re)connection.
        """
        self._access_token = access_token
        self._expires_at = expires_at
        _LOGGER.debug("API: access token updated (expires_at=%s)", expires_at)

    async def get_devices(
        self,
        page: int | None = 1,
        pageLength: int | None = 100,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Get list of devices associated with the account.

        Raises:
            TokenExpired: When the access token has expired (401).
            Unauthorized: When the request is unauthorized (403).
            DevicesNotFound: When no devices are found (404).
            RateLimited: When the request is rate limited (429).
            ServerError: When the server returns an internal error (500).
            OlarmFlowClientApiError: For other API errors.
        """
        params = {
            "page": page,
            "pageLength": pageLength,
            "search": search,
            "deviceApiAccessOnly": "1",
        }

        try:
            return await self._api_make_request("GET", "/api/v4/devices", params=params)
        except OlarmFlowClientApiError as err:
            # Handle specific status codes
            if err.status_code == 404:
                raise DevicesNotFound() from err
            # Handle common status codes (401, 403, 500) or re-raise
            self._handle_api_error(err)
            raise  # This line is never reached but satisfies mypy

    async def get_device(self, device_id: str) -> dict[str, Any]:
        """Get a specific device associated with the account.

        Raises:
            TokenExpired: When the access token has expired (401).
            DeviceNotFound: When the device is not found or not accessible (404, 403).
            RateLimited: When the request is rate limited (429).
            ServerError: When the server returns an internal error (500).
            OlarmFlowClientApiError: For other API errors.
        """
        try:
            return await self._api_make_request(
                "GET",
                f"/api/v4/devices/{device_id}",
                params={"deviceApiAccessOnly": "1"},
            )
        except OlarmFlowClientApiError as err:
            # Handle specific status codes
            if err.status_code == 404 or err.status_code == 403:
                # Both 404 and 403 mean the device is not accessible/not found
                raise DeviceNotFound(device_id) from err
            # Handle other common status codes (401, 500) or re-raise
            self._handle_api_error(err)
            raise  # This line is never reached but satisfies mypy

    async def get_device_actions(self, device_id: str) -> dict[str, Any]:
        """Get list of past actions for a specific device."""
        return await self._api_make_request(
            "GET", f"/api/v4/devices/{device_id}/actions"
        )

    async def get_device_events(
        self,
        device_id: str,
        limit: int | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Get list of events for a specific device."""
        params = {"limit": limit, "after": after}
        return await self._api_make_request(
            "GET", f"/api/v4/devices/{device_id}/events", params=params
        )

    async def send_device_area_disarm(
        self, device_id: str, area_num: int
    ) -> dict[str, Any]:
        """Disarm a device area."""
        return await self._api_send_action(device_id, "area-disarm", area_num)

    async def send_device_area_arm(
        self, device_id: str, area_num: int
    ) -> dict[str, Any]:
        """Arm a device area fully."""
        return await self._api_send_action(device_id, "area-arm", area_num)

    async def send_device_area_part_arm(
        self, device_id: str, area_num: int, part_num: int
    ) -> dict[str, Any]:
        """Arm a device area partially."""
        action_cmd = f"area-part-arm-{part_num}"
        return await self._api_send_action(device_id, action_cmd, area_num)

    async def send_device_area_custom_arm(
        self, device_id: str, area_num: int, part_num: int
    ) -> dict[str, Any]:
        """Arm a device area with custom partial profile (Olarm ONE HUB)."""
        action_cmd = f"area-custom-arm-{part_num}"
        return await self._api_send_action(device_id, action_cmd, area_num)

    async def send_device_area_stay(
        self, device_id: str, area_num: int
    ) -> dict[str, Any]:
        """Set a device area to stay armed."""
        return await self._api_send_action(device_id, "area-stay", area_num)

    async def send_device_area_sleep(
        self, device_id: str, area_num: int
    ) -> dict[str, Any]:
        """Set a device area to sleep armed."""
        return await self._api_send_action(device_id, "area-sleep", area_num)

    async def send_device_zone_bypass(
        self, device_id: str, zone_num: int
    ) -> dict[str, Any]:
        """Bypass a device zone."""
        return await self._api_send_action(device_id, "zone-bypass", zone_num)

    async def send_device_zone_unbypass(
        self, device_id: str, zone_num: int
    ) -> dict[str, Any]:
        """Unbypass a device zone."""
        return await self._api_send_action(device_id, "zone-unbypass", zone_num)

    async def send_device_pgm_open(
        self, device_id: str, pgm_num: int
    ) -> dict[str, Any]:
        """Set a device PGM output to open."""
        return await self._api_send_action(device_id, "pgm-open", pgm_num)

    async def send_device_pgm_close(
        self, device_id: str, pgm_num: int
    ) -> dict[str, Any]:
        """Set a device PGM output to close."""
        return await self._api_send_action(device_id, "pgm-close", pgm_num)

    async def send_device_pgm_pulse(
        self, device_id: str, pgm_num: int
    ) -> dict[str, Any]:
        """Pulse a device PGM output."""
        return await self._api_send_action(device_id, "pgm-pulse", pgm_num)

    async def send_device_ukey_activate(
        self, device_id: str, ukey_num: int
    ) -> dict[str, Any]:
        """Activate a device utility key."""
        return await self._api_send_action(device_id, "ukey-activate", ukey_num)

    async def send_device_link_output_open(
        self, device_id: str, link_id: str, output_num: int
    ) -> dict[str, Any]:
        """Open an Olarm LINK output."""
        return await self._api_send_action(
            device_id, "link-io-open", output_num, link_id
        )

    async def send_device_link_output_close(
        self, device_id: str, link_id: str, output_num: int
    ) -> dict[str, Any]:
        """Close an Olarm LINK output."""
        return await self._api_send_action(
            device_id, "link-io-close", output_num, link_id
        )

    # NOTE: output close cutoff will be implemented in the future
    # async def send_device_link_output_close_cutoff(
    #     self, device_id: str, link_id: str, output_num: int
    # ) -> dict[str, Any]:
    #     """Close an Olarm LINK output with cutoff."""
    #     return await self._api_send_action(
    #         device_id, "link-io-close-cutoff", output_num, link_id
    #     )

    async def send_device_link_output_pulse(
        self, device_id: str, link_id: str, output_num: int
    ) -> dict[str, Any]:
        """Pulse an Olarm LINK output."""
        return await self._api_send_action(
            device_id, "link-io-pulse", output_num, link_id
        )

    async def send_device_link_relay_unlatch(
        self, device_id: str, link_id: str, relay_num: int
    ) -> dict[str, Any]:
        """Unlatch an Olarm LINK Relay."""
        return await self._api_send_action(
            device_id, "link-relay-unlatch", relay_num, link_id
        )

    async def send_device_link_relay_latch(
        self, device_id: str, link_id: str, relay_num: int
    ) -> dict[str, Any]:
        """Latch an Olarm LINK Relay."""
        return await self._api_send_action(
            device_id, "link-relay-latch", relay_num, link_id
        )

    # NOTE: relay latch cutoff will be implemented in the future
    # async def send_device_link_relay_latch_cutoff(
    #     self, device_id: str, link_id: str, relay_num: int
    # ) -> dict[str, Any]:
    #     """Latch an Olarm LINK Relay with cutoff."""
    #     return await self._api_send_action(
    #         device_id, "link-relay-latch-cutoff", relay_num, link_id
    #     )

    async def send_device_link_relay_pulse(
        self, device_id: str, link_id: str, relay_num: int
    ) -> dict[str, Any]:
        """Pulse an Olarm LINK Relay."""
        return await self._api_send_action(
            device_id, "link-relay-pulse", relay_num, link_id
        )

    async def send_device_max_output_open(
        self, device_id: str, output_num: int
    ) -> dict[str, Any]:
        """Open an Olarm MAX output."""
        return await self._api_send_action(device_id, "max-io-open", output_num)

    async def send_device_max_output_close(
        self, device_id: str, output_num: int
    ) -> dict[str, Any]:
        """Close an Olarm MAX output."""
        return await self._api_send_action(device_id, "max-io-close", output_num)

    async def send_device_max_output_pulse(
        self, device_id: str, output_num: int
    ) -> dict[str, Any]:
        """Pulse an Olarm MAX output."""
        return await self._api_send_action(device_id, "max-io-pulse", output_num)

    async def send_user_panic(self, device_id: str) -> dict[str, Any]:
        """Send User Panic."""
        return await self._api_send_action(device_id, "user-panic", 0)

    async def start_mqtt_async(
        self,
        user_id: str,
        client_id_suffix: str | None = "1",
        timeout: float = 30.0,
    ) -> None:
        """Start the MQTT client and wait for the first connection.

        Performs the first connect (and subscribe of any registered topics)
        before returning, so connection and authentication failures surface
        here. On success a background reconnect loop keeps the connection
        alive, re-authenticating with the current access token and
        re-subscribing on every reconnect.

        Args:
            user_id: Olarm user id, used to build the MQTT client id.
            client_id_suffix: Suffix for the MQTT client id.
            timeout: Seconds to wait for the first connection.

        Raises:
            MqttAuthError: If the broker refuses the connection due to bad
                credentials (subclass of MqttConnectError).
            MqttConnectError: If connection to MQTT broker fails.
            MqttTimeoutError: If connection times out.
            RuntimeError: If already running with a different client id;
                call stop_mqtt() first to change parameters.
        """
        if self._mqtt_task is not None and not self._mqtt_task.done():
            requested_client_id = f"{user_id}-{client_id_suffix}"
            if requested_client_id != self._mqtt_clientId:
                raise RuntimeError(
                    f"MQTT client already running as '{self._mqtt_clientId}'; "
                    "call stop_mqtt() before starting with different parameters"
                )
            _LOGGER.debug("MQTT: client already running")
            return

        loop = asyncio.get_running_loop()
        self._event_loop = loop
        self._mqtt_clientId = f"{user_id}-{client_id_suffix}"
        self._mqtt_retries = 0

        _LOGGER.debug(
            "MQTT: starting client over websockets (client_id=%s, host=%s, port=%s)",
            self._mqtt_clientId,
            MQTT_HOST,
            MQTT_PORT,
        )

        first_connect: asyncio.Future[None] = loop.create_future()
        self._mqtt_task = loop.create_task(self._mqtt_loop(first_connect))

        try:
            await asyncio.wait_for(first_connect, timeout=timeout)
        except asyncio.TimeoutError as e:
            _LOGGER.debug("MQTT: connection timed out (timeout=%.0fs)", timeout)
            # Retrieve any raced exception to silence asyncio's "never retrieved" warning
            if first_connect.done() and not first_connect.cancelled():
                first_connect.exception()
            self.stop_mqtt()
            raise MqttTimeoutError("MQTT connection timeout") from e
        except MqttConnectError:
            self.stop_mqtt()
            raise

    async def _mqtt_loop(self, first_connect: asyncio.Future[None]) -> None:
        """Connect/reconnect loop following the aiomqtt reconnect pattern.

        A fresh connection is built for every attempt using the *current*
        access token, so tokens rotated via ``update_access_token()`` are
        picked up automatically.
        """
        try:
            while True:
                self._call_status_callback("connecting", {})
                try:
                    async with self._make_mqtt_client() as client:
                        self._mqtt_client = client
                        for topic in self._mqtt_callbacks:
                            _LOGGER.debug("MQTT: (re)subscribing (topic=%s)", topic)
                            await client.subscribe(topic)
                        self._mqtt_retries = 0
                        _LOGGER.debug("MQTT: connected to broker")
                        if not first_connect.done():
                            first_connect.set_result(None)
                        self._call_status_callback("connected", {})
                        async for message in client.messages:
                            self._mqtt_dispatch(str(message.topic), message.payload)
                except aiomqtt.MqttError as err:
                    self._mqtt_client = None
                    if not first_connect.done():
                        # First connect failed: surface the error through
                        # start_mqtt_async() and don't retry
                        first_connect.set_exception(self._map_mqtt_error(err))
                        return
                    self._mqtt_retries += 1
                    reason = str(err)
                    info = self._mqtt_error_info(err)
                    # Report "disconnected" once at the threshold; later failures stay "reconnecting"
                    if self._mqtt_retries == self._mqtt_retries_before_disconnect:
                        _LOGGER.error(
                            "MQTT: connection lost (retries=%d): %s",
                            self._mqtt_retries,
                            reason,
                        )
                        self._call_status_callback("disconnected", info)
                    else:
                        _LOGGER.debug(
                            "MQTT: connection lost, reconnecting (retries=%d): %s",
                            self._mqtt_retries,
                            reason,
                        )
                        self._call_status_callback("reconnecting", info)
                    delay = min(
                        MQTT_RECONNECT_BACKOFF_MIN * 2 ** (self._mqtt_retries - 1),
                        MQTT_RECONNECT_BACKOFF_MAX,
                    )
                    await asyncio.sleep(delay)
        finally:
            self._mqtt_client = None

    def _make_mqtt_client(self) -> aiomqtt.Client:
        """Build a new aiomqtt client using the current access token."""
        return aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            username=MQTT_USER,
            password=self._access_token,
            identifier=self._mqtt_clientId,
            transport="websockets",
            websocket_path="/mqtt",
            tls_context=ssl.create_default_context(),
            keepalive=MQTT_KEEPALIVE,
        )

    @staticmethod
    def _map_mqtt_error(err: aiomqtt.MqttError) -> MqttConnectError:
        """Translate an aiomqtt error into this library's exception hierarchy."""
        if isinstance(err, aiomqtt.MqttCodeError):
            rc = getattr(err.rc, "value", err.rc)
            # CONNACK reason codes indicating an authentication/authorization
            # failure: MQTT v3 codes (4: bad username/password, 5: not
            # authorised) and their MQTT v5 equivalents (134, 135)
            if rc in {4, 5, 134, 135}:
                return MqttAuthError(f"MQTT authentication failed: {err}")
        return MqttConnectError(f"MQTT connection failed: {err}")

    @classmethod
    def _mqtt_error_info(cls, err: aiomqtt.MqttError) -> dict[str, Any]:
        """Build the status callback info dict for a connection failure.

        Includes ``reason`` (free text), ``rc`` (CONNACK reason code, or None
        for non-CONNACK errors such as network drops) and ``auth`` (True if
        the failure was an authentication/authorization refusal, e.g. an
        expired access token).
        """
        rc = None
        if isinstance(err, aiomqtt.MqttCodeError):
            rc = getattr(err.rc, "value", err.rc)
        return {
            "reason": str(err),
            "rc": rc,
            "auth": isinstance(cls._map_mqtt_error(err), MqttAuthError),
        }

    def stop_mqtt(self) -> None:
        """Stop and disconnect MQTT.

        Idempotent, safe to call from any thread and never blocks.
        """
        task = self._mqtt_task
        self._mqtt_task = None
        self._mqtt_retries = 0
        if task is None or task.done():
            _LOGGER.debug("MQTT: client was not running")
            return
        task.get_loop().call_soon_threadsafe(task.cancel)
        _LOGGER.debug("MQTT: client stopped")

    def set_mqtt_status_callback(
        self,
        callback: Callable[
            [
                Literal["connecting", "connected", "disconnected", "reconnecting"],
                dict[str, Any],
            ],
            None,
        ],
    ) -> None:
        """Set a callback to be called when MQTT connection status changes."""
        self._mqtt_status_callback = callback

    def _call_status_callback(
        self,
        status: Literal["connecting", "connected", "disconnected", "reconnecting"],
        info: dict[str, Any],
    ) -> None:
        """Call the connection status callback, shielding the loop from errors."""
        if self._mqtt_status_callback is None:
            return
        try:
            self._mqtt_status_callback(status, info)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "MQTT: status callback raised an exception (status=%s)", status
            )

    def subscribe_to_device(
        self, device_id: str, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Subscribe to a specific device's topics."""
        self._mqtt_subscribe(f"v4/devices/{device_id}", callback)

    def _mqtt_subscribe(
        self, topic: str, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Register a topic callback and subscribe if currently connected.

        If not connected, the reconnect loop subscribes to all registered
        topics on the next (re)connect.
        """
        self._mqtt_callbacks[topic] = callback
        client = self._mqtt_client
        loop = self._event_loop
        if client is not None and loop is not None:
            _LOGGER.debug("MQTT: subscribing (topic=%s)", topic)

            def _schedule() -> None:
                task = loop.create_task(self._mqtt_subscribe_now(client, topic))
                self._mqtt_bg_tasks.add(task)
                task.add_done_callback(self._mqtt_bg_tasks.discard)

            loop.call_soon_threadsafe(_schedule)
        else:
            _LOGGER.debug(
                "MQTT: subscription queued until client connects (topic=%s)", topic
            )

    async def _mqtt_subscribe_now(self, client: aiomqtt.Client, topic: str) -> None:
        """Subscribe to a topic on a live connection."""
        try:
            await client.subscribe(topic)
        except aiomqtt.MqttError as err:
            # The reconnect loop re-subscribes on the next connect
            _LOGGER.debug("MQTT: live subscribe failed (topic=%s): %s", topic, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("MQTT: unexpected error subscribing (topic=%s)", topic)

    def _mqtt_dispatch(self, topic: str, payload: Any) -> None:
        """Decode a message payload and dispatch it to the registered callback."""
        callback = self._mqtt_callbacks.get(topic)
        if callback is None:
            return
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode()
            data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            _LOGGER.error(
                "MQTT: failed to decode message payload (topic=%s): %s", topic, payload
            )
            return
        try:
            callback(topic, data)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("MQTT: error processing message (topic=%s)", topic)
