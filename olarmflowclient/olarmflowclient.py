"""Olarm API - The official async Python client for interacting with the Olarm HTTP API.

See https://www.olarm.com for more info.
"""

from collections.abc import Callable
import json
import logging
import ssl
from typing import Any, Literal
import urllib.parse

import aiohttp
import paho.mqtt.client as mqtt

from .const import BASE_URL, MQTT_HOST, MQTT_KEEPALIVE, MQTT_PORT, MQTT_USER

_LOGGER = logging.getLogger(__name__)

# Threshold: number of consecutive failures before emitting a "disconnected" status
MQTT_RETRIES_BEFORE_DISCONNECT = 3


class OlarmFlowClientApiError(Exception):
    """Raised when the API returns an error."""

    # Standard HTTP error descriptions
    HTTP_ERROR_DESCRIPTIONS = {
        403: "Unauthorized",
        429: "Request was rate limited",
        500: "Olarm server error",
    }

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_text: str | None = None,
    ) -> None:
        """Initialize the API error."""
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.status_code:
            error_desc = self.HTTP_ERROR_DESCRIPTIONS.get(self.status_code, "")
            if error_desc:
                return f"API Error {self.status_code} ({error_desc}): {super().__str__()} - {self.response_text}"
            return f"API Error {self.status_code}: {super().__str__()} - {self.response_text}"
        return super().__str__()


class TokenExpired(OlarmFlowClientApiError):
    """Raised when the access token has expired (401)."""

    def __init__(self, message: str = "Access token has expired") -> None:
        """Initialize the token expired error."""
        super().__init__(message, status_code=401)


class Unauthorized(OlarmFlowClientApiError):
    """Raised when the request is unauthorized (403)."""

    def __init__(self, message: str = "Unauthorized access") -> None:
        """Initialize the unauthorized error."""
        super().__init__(message, status_code=403)


class DeviceNotFound(OlarmFlowClientApiError):
    """Raised when a specific device is not found or not accessible (404, 403)."""

    def __init__(self, device_id: str | None = None) -> None:
        """Initialize the device not found error."""
        message = f"Device '{device_id}' not found" if device_id else "Device not found"
        super().__init__(message, status_code=404)


class DevicesNotFound(OlarmFlowClientApiError):
    """Raised when no devices are found for the account (404)."""

    def __init__(self, message: str = "No devices found for this account") -> None:
        """Initialize the devices not found error."""
        super().__init__(message, status_code=404)


class ServerError(OlarmFlowClientApiError):
    """Raised when the server returns an internal error (500)."""

    def __init__(self, message: str = "Server internal error") -> None:
        """Initialize the server error."""
        super().__init__(message, status_code=500)


class RateLimited(OlarmFlowClientApiError):
    """Raised when the request is rate limited (429)."""

    def __init__(self, message: str = "Too many requests - rate limited") -> None:
        """Initialize the rate limited error."""
        super().__init__(message, status_code=429)


class MqttConnectError(OlarmFlowClientApiError):
    """Raised when MQTT connection fails."""

    def __init__(self, message: str = "MQTT connection failed") -> None:
        """Initialize the MQTT connection error."""
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
        expires_at: int | None = None,
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
        self._mqtt_host: str | None = None
        self._mqtt_port: int | None = None
        self._mqtt_username: str | None = None
        self._mqtt_password: str | None = None
        self._mqtt_clientId: str | None = None
        self._mqtt_client: mqtt.Client | None = None
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
        self._event_loop: Any = None

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

        result: dict[str, Any] = {}
        try:
            async with self._api_session.request(method, url, **kwargs) as response:
                if response.status != 200:
                    text = await response.text()
                    raise OlarmFlowClientApiError(
                        "Request failed",
                        status_code=response.status,
                        response_text=text,
                    )

                if "application/json" in response.headers.get("Content-Type", ""):
                    result = await response.json()
                else:
                    result = await response.text()

        except aiohttp.ClientError as e:
            raise OlarmFlowClientApiError(f"API request failed: {e!s}") from e
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
        if err.status_code == 401:
            raise TokenExpired() from err
        elif err.status_code == 403:
            raise Unauthorized() from err
        elif err.status_code == 429:
            raise RateLimited() from err
        elif err.status_code == 500:
            raise ServerError() from err
        else:
            # Re-raise original error for other status codes
            raise err

    async def update_access_token(self, access_token: str, expires_at: int) -> None:
        """Update the access token."""
        self._access_token = access_token
        self._expires_at = expires_at
        _LOGGER.debug("Updating access_token")

        # Update MQTT password if MQTT client is active
        if self._mqtt_client is not None:
            self._mqtt_password = self._access_token
            # Update the credentials for the MQTT client
            self._mqtt_client.username_pw_set(self._mqtt_username, self._mqtt_password)

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

    def start_mqtt(
        self,
        user_id: str,
        client_id_suffix: str | None = "1",
    ) -> None:
        """Start the MQTT client."""

        # mqtt client
        self._mqtt_host = MQTT_HOST
        self._mqtt_port = MQTT_PORT
        self._mqtt_username = MQTT_USER
        self._mqtt_password = self._access_token
        self._mqtt_clientId = f"{user_id}-{client_id_suffix}"

        # Create SSL context
        ssl_context = ssl.create_default_context()

        # Initialize MQTT client with websockets transport
        _LOGGER.debug(
            "Starting MQTT client over websockets with clientId: %s",
            self._mqtt_clientId,
        )
        self._mqtt_client = mqtt.Client(
            client_id=self._mqtt_clientId, transport="websockets"
        )
        self._mqtt_client.tls_set_context(ssl_context)
        self._mqtt_client.tls_insecure_set(False)

        # Set websocket path and headers
        self._mqtt_client.ws_set_options(
            path="/mqtt",
        )

        # track callbacks for subscriptions
        self._mqtt_callbacks = {}

        # setup options
        self._mqtt_client.username_pw_set(self._mqtt_username, self._mqtt_password)
        self._mqtt_client.reconnect_delay_set(min_delay=4, max_delay=60)

        # Set up callbacks before connecting
        self._mqtt_client.on_connect = self._mqtt_on_connect
        self._mqtt_client.on_disconnect = self._mqtt_on_disconnect
        self._mqtt_client.on_message = self._mqtt_on_message

        # connect to the broker using connect_async for non-blocking connection
        try:
            # Notify that we are starting the connection process
            self._call_status_callback("connecting", {})
            self._mqtt_client.connect_async(
                self._mqtt_host, self._mqtt_port, keepalive=MQTT_KEEPALIVE
            )
            self._mqtt_client.loop_start()

        except Exception as e:
            _LOGGER.error("Failed to connect to MQTT broker: %s", e)
            raise MqttConnectError(f"Failed to connect to MQTT broker: {e}") from e

    async def start_mqtt_async(
        self,
        user_id: str,
        client_id_suffix: str | None = "1",
        event_loop: Any = None,
        timeout: float = 30.0,
    ) -> None:
        """Start the MQTT client asynchronously with thread-safe handling.

        Raises:
            MqttConnectError: If connection to MQTT broker fails.
            MqttTimeoutError: If connection times out.
        """
        import asyncio

        # Store event loop for thread-safe callbacks
        if event_loop is not None:
            self._event_loop = event_loop
        else:
            self._event_loop = asyncio.get_event_loop()

        # Run start_mqtt in executor with timeout
        try:
            await asyncio.wait_for(
                self._event_loop.run_in_executor(
                    None, self.start_mqtt, user_id, client_id_suffix
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            _LOGGER.error("Timeout connecting to Olarm MQTT Service")
            raise MqttTimeoutError("MQTT connection timeout") from e
        except MqttConnectError:
            # Re-raise MQTT connect errors as-is
            raise
        except Exception as e:
            _LOGGER.error("Failed to start MQTT client: %s", e)
            raise MqttConnectError(f"Failed to start MQTT client: {e}") from e

    def stop_mqtt(self) -> None:
        """Stop and disconnect MQTT."""
        if self._mqtt_client is not None:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
                self._mqtt_retries = 0
                _LOGGER.debug("MQTT client stopped and disconnected")
            except Exception as e:
                _LOGGER.warning("Error stopping MQTT client: %s", e)
        else:
            _LOGGER.debug("MQTT client was not running")

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
        """Call the connection status callback in a thread-safe manner."""
        if self._mqtt_status_callback is None:
            return

        # Use thread-safe scheduling if event loop is available
        if self._event_loop is not None:
            self._event_loop.call_soon_threadsafe(
                self._mqtt_status_callback, status, info
            )
        else:
            try:
                self._mqtt_status_callback(status, info)
            except (OSError, ValueError, TypeError) as e:
                _LOGGER.error("Error in connection status callback: %s", e)

    def _handle_connection_failure(self, rc: int, is_disconnect: bool = False) -> None:
        """Handle connection failure and determine appropriate status.

        Args:
            rc: MQTT return code
            is_disconnect: True if called from on_disconnect, False if from on_connect
        """
        # MQTT return code descriptions
        reason_map = {
            1: "Connection refused - incorrect protocol version",
            2: "Connection refused - invalid client identifier",
            3: "Connection refused - server unavailable",
            4: "Connection refused - bad username or password",
            5: "Connection refused - not authorised",
            6: "Connection refused - TLS handshake failed",
            7: "Connection refused - possibly server closed connection",
        }
        reason = reason_map.get(rc, f"Unknown error (rc={rc})")

        # Define error categories
        unrecoverable_codes = {1, 2, 6}  # Protocol, client ID, TLS
        auth_codes = {4, 5, 7}  # Bad credentials, not authorized, connection closed

        # Determine status based on error code and retry count
        if rc in unrecoverable_codes:
            # Unrecoverable errors trigger immediate "disconnected"
            _LOGGER.error("MQTT unrecoverable error: %s", reason)
            self._call_status_callback("disconnected", {"reason": reason, "rc": rc})

        elif self._mqtt_retries >= self._mqtt_retries_before_disconnect:
            # Max retries reached, trigger "disconnected"
            _LOGGER.error(
                "MQTT reconnections failed: %s (retries: %d)",
                reason,
                self._mqtt_retries,
            )
            self._call_status_callback("disconnected", {"reason": reason, "rc": rc})

        elif rc in auth_codes:
            # Auth-related errors may be recoverable (e.g., token rotation)
            _LOGGER.debug("MQTT needs to reconnect (possibly refresh token)")
            self._call_status_callback("reconnecting", {"reason": reason, "rc": rc})

        else:
            # All other errors 
            _LOGGER.debug(
                "MQTT connection lost: %s (retries: %d)",
                reason,
                self._mqtt_retries,
            )
            self._call_status_callback("disconnected", {"reason": reason, "rc": rc})

    def subscribe_to_device(
        self, device_id: str, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Subscribe to a specific device's topics."""
        self._mqtt_subscribe(f"v4/devices/{device_id}", callback)

    def _mqtt_subscribe(
        self, topic: str, callback: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """Subscribe to a topic and register a callback."""
        self._mqtt_callbacks[topic] = callback
        # Only attempt to subscribe if the client is currently connected.
        # If not connected, the subscription will happen in _on_connect.
        if self._mqtt_client is not None and self._mqtt_client.is_connected():
            _LOGGER.debug("Subscribing to topic: %s", topic)
            self._mqtt_client.subscribe(topic)
        else:
            _LOGGER.debug(
                "Subscribing to topic queued for when client connects: %s",
                topic,
            )

    def _mqtt_on_connect(
        self, client: mqtt.Client, userdata: Any, flags: dict[str, Any], rc: int
    ) -> None:
        """Handle connection to the broker."""
        if rc == 0:
            _LOGGER.debug("Connected to MQTT broker")
            # Reset retry counter on successful connection
            self._mqtt_retries = 0
            # Resubscribe to all topics in callback registry on reconnect
            if self._mqtt_client is not None:
                for topic in self._mqtt_callbacks:
                    _LOGGER.debug("(Re)Subscribing to topic: %s", topic)
                    self._mqtt_client.subscribe(topic)

            # Notify callback of successful connection
            self._call_status_callback("connected", {})
        else:
            _LOGGER.error("Failed to connect to MQTT broker with code: %s", rc)
            # Increment retry counter
            self._mqtt_retries += 1
            # Handle the failure using consolidated logic
            self._handle_connection_failure(rc, is_disconnect=False)

    def _mqtt_on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Handle disconnection from the broker."""
        if rc == 0:
            # Clean disconnect (user initiated)
            _LOGGER.debug("Disconnected from MQTT broker (clean disconnect)")
            # Reset retry counter on clean disconnect
            self._mqtt_retries = 0
        else:
            # Unexpected disconnect
            _LOGGER.warning("Disconnected from MQTT broker with code: %s", rc)
            # Increment retry counter on unexpected disconnect
            self._mqtt_retries += 1
            # Handle the failure using consolidated logic
            self._handle_connection_failure(rc, is_disconnect=True)

    def _mqtt_on_message(
        self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        """Handle messages received from the broker."""
        # _LOGGER.debug("Received message on topic: %s", message.topic)
        try:
            payload = json.loads(message.payload.decode())
            if message.topic in self._mqtt_callbacks:
                self._mqtt_callbacks[message.topic](message.topic, payload)
        except json.JSONDecodeError:
            _LOGGER.error("Failed to decode message payload: %s", message.payload)
        except (ValueError, TypeError, KeyError) as err:
            _LOGGER.error("Error processing message: %s", err)
