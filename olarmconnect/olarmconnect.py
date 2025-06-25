"""Olarm API - The official async Python client for interacting with the Olarm HTTP API.

See https://www.olarm.com for more info.
"""

from collections.abc import Callable
import json
import logging
import ssl
from typing import Any
import urllib.parse

import aiohttp
import paho.mqtt.client as mqtt

from .const import BASE_URL, MQTT_HOST, MQTT_KEEPALIVE, MQTT_PORT, MQTT_USER

_LOGGER = logging.getLogger(__name__)


class OlarmConnectApiError(Exception):
    """Raised when the API returns an error."""

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
            return f"API Error {self.status_code}: {super().__str__()} - {self.response_text}"
        return super().__str__()


class OlarmConnect:
    """Async client class for interacting with the Olarm API."""

    def __init__(
        self,
        access_token: str,
        expires_at: int | None = None,
    ) -> None:
        """Initialize the Olarm Connect client."""

        # tokens
        self._access_token = access_token
        self._expires_at = expires_at
        self._is_jwt_token = len(self._access_token.split(".")) == 3

        # api client attributes (initialized to None)
        self._api_session = None

        # mqtt client attributes (initialized to None)
        self._mqtt_host: str | None = None
        self._mqtt_port: int | None = None
        self._mqtt_username: str | None = None
        self._mqtt_password: str | None = None
        self._mqtt_clientId: str | None = None
        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_callbacks: dict[str, Callable[[str, dict[str, Any]], None]] = {}
        self._mqtt_reconnection_callback: Callable[[], None] | None = None

    async def __aenter__(self):
        """Async context manager enter."""
        await self._api_connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
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
        **kwargs,
    ) -> dict[str, Any]:
        """Make an authenticated request to the API."""

        await self._api_connect()

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

        result = None
        try:
            async with self._api_session.request(method, url, **kwargs) as response:
                if response.status != 200:
                    text = await response.text()
                    raise OlarmConnectApiError(
                        "Request failed",
                        status_code=response.status,
                        response_text=text,
                    )

                if "application/json" in response.headers.get("Content-Type", ""):
                    result = await response.json()
                else:
                    result = await response.text()

        except aiohttp.ClientError as e:
            raise OlarmConnectApiError(f"API request failed: {e!s}") from e
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
        """Get list of devices associated with the account."""
        params = {
            "page": page,
            "pageLength": pageLength,
            "search": search,
            "deviceApiAccessOnly": "1",
        }
        return await self._api_make_request("GET", "/api/v4/devices", params=params)

    async def get_device(self, device_id: str) -> dict[str, Any]:
        """Get a specific device associated with the account."""
        return await self._api_make_request(
            "GET",
            f"/api/v4/devices/{device_id}",
            params={"deviceApiAccessOnly": "1"},
        )

    async def get_device_actions(self, device_id: str) -> list[dict[str, Any]]:
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
        ssl_context: ssl.SSLContext | None = None,
        client_id_suffix: str | None = "1",
    ) -> None:
        """Start the MQTT client."""

        # mqtt client
        self._mqtt_host = MQTT_HOST
        self._mqtt_port = MQTT_PORT
        self._mqtt_username = MQTT_USER
        self._mqtt_password = self._access_token
        self._mqtt_clientId = f"{user_id}-{client_id_suffix}"

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
        self._mqtt_client.reconnect_delay_set(min_delay=8, max_delay=60)

        # connect to the broker
        try:
            # Set up callbacks before connecting
            self._mqtt_client.on_connect = self._mqtt_on_connect
            self._mqtt_client.on_disconnect = self._mqtt_on_disconnect
            self._mqtt_client.on_message = self._mqtt_on_message

            # Use connect_async for non-blocking connection
            self._mqtt_client.connect_async(
                self._mqtt_host, self._mqtt_port, keepalive=MQTT_KEEPALIVE
            )
            self._mqtt_client.loop_start()

        except Exception as e:
            _LOGGER.error("Failed to connect to MQTT broker: %s", e)
            raise

    def stop_mqtt(self) -> None:
        """Stop and disconnect MQTT."""
        if self._mqtt_client is not None:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            _LOGGER.debug("MQTT client stopped and disconnected")
        else:
            _LOGGER.debug("MQTT client was not running")

    def set_mqtt_reconnection_callback(self, callback: Callable[[], None]) -> None:
        """Set a callback to be called when MQTT needs to reconnect."""
        self._mqtt_reconnection_callback = callback

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
        if self._mqtt_client.is_connected():
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
            # Resubscribe to all topics in callback registry on reconnect
            for topic in self._mqtt_callbacks:
                _LOGGER.debug("(Re)Subscribing to topic: %s", topic)
                self._mqtt_client.subscribe(topic)
        else:
            _LOGGER.error("Failed to connect to MQTT broker with code: %s", rc)

    def _mqtt_on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Handle disconnection from the broker."""
        reason_map = {
            0: "Client initiated disconnect",
            1: "Connection refused - incorrect protocol version",
            2: "Connection refused - invalid client identifier",
            3: "Connection refused - server unavailable",
            4: "Connection refused - bad username or password",
            5: "Connection refused - not authorised",
            6: "Connection refused - TLS handshake failed",
            7: "Connection refused - possibly server closed connection",
        }
        reason = reason_map.get(rc, f"Unknown reason (rc={rc})")
        if rc == 0:
            _LOGGER.debug("Disconnected from MQTT broker: %s [%s]", reason, rc)
        else:
            _LOGGER.warning("Disconnected from MQTT broker: %s [%s]", reason, rc)

        # callback used to refresh tokens if using oauth
        if rc in (4, 5, 7) and self._mqtt_reconnection_callback is not None:
            _LOGGER.debug("Calling reconnection callback to refresh token if necessary")
            try:
                self._mqtt_reconnection_callback()
            except (OSError, ValueError, TypeError) as e:
                _LOGGER.error("Error in reconnection callback: %s", e)

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
