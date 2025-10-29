import asyncio
import logging
import argparse
import sys
from pathlib import Path

# Add parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))

from olarmflowclient import (
    OlarmFlowClient,
    OlarmFlowClientApiError,
    DevicesNotFound,
    MqttConnectError,
    MqttTimeoutError,
)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
_LOGGER = logging.getLogger(__name__)


def mqtt_reconnection_callback():
    """Callback function for MQTT reconnection events.

    This is called by the library when MQTT disconnects due to authentication issues.
    For OAuth tokens, this would trigger a token refresh.
    For classic API tokens, this just logs the reconnection attempt.
    """
    _LOGGER.info("MQTT reconnection callback triggered - authentication issue detected")
    # In a real OAuth implementation, you would refresh the token here
    # For example: await client.update_access_token(new_token, expires_at)


async def main(api_token):
    try:
        async with OlarmFlowClient(api_token) as client:
            # Get all devices
            _LOGGER.info("Fetching devices...")
            devices_result = await client.get_devices()

            # check result
            user_id = devices_result.get("userId")
            devices = devices_result.get("data", [])
            if len(devices) == 0:
                _LOGGER.error("No devices found")
                return
            _LOGGER.info(f"Found {len(devices)} devices.")

            # Add checks for type and content
            if not isinstance(devices, list) or not devices:
                _LOGGER.error("No devices found or unexpected data format received.")
                return  # Exit if no devices or wrong format

            # Ensure user_id is not None
            if user_id is None:
                _LOGGER.error("No user ID found in devices result")
                return

            # Set up MQTT reconnection callback before connecting
            # This will be called if MQTT disconnects due to authentication errors
            _LOGGER.info("Setting up MQTT reconnection callback...")
            client.set_mqtt_reconnection_callback(mqtt_reconnection_callback)

            # Connect to MQTT broker using async method
            # (using client_id_suffix="6" to avoid conflicts if user is already setup a client)
            _LOGGER.info("Connecting to MQTT broker asynchronously...")
            try:
                await client.start_mqtt_async(
                    user_id=user_id,
                    client_id_suffix="5",
                    event_loop=asyncio.get_running_loop(),
                    timeout=10.0,
                )
                _LOGGER.info("Successfully connected to MQTT broker")
            except MqttTimeoutError as e:
                _LOGGER.error(f"Timeout connecting to MQTT: {e}")
                return
            except MqttConnectError as e:
                _LOGGER.error(f"Failed to connect to MQTT: {e}")
                return

            # Subscribe to Device Events (first 8 of them)
            for device in devices[:8]:
                client.subscribe_to_device(device.get("deviceId"), message_callback)
                _LOGGER.info(f"subscribing to -> v4/devices/{device.get('deviceId')}")

            # Keep the async loop alive
            _LOGGER.info("MQTT client running. Press Ctrl+C to stop.")
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                _LOGGER.info("Received cancellation, stopping MQTT...")
                raise

    except DevicesNotFound as e:
        _LOGGER.error(f"No devices found for this account: {e}")
    except OlarmFlowClientApiError as e:
        _LOGGER.error(f"API Error occurred: {e}")
    except Exception as e:
        _LOGGER.exception(f"Unexpected error occurred: {e}")


# Define callback function for MQTT messages
def message_callback(topic, payload):
    _LOGGER.info("Received MQTT message:")
    _LOGGER.info(f"Topic: {topic}")
    _LOGGER.info(f"Payload: {payload}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Olarm Flow Client Example - Subscribe to Device using MQTT (Async)"
    )
    parser.add_argument("--api-token", required=True, help="Your Olarm API token")

    args = parser.parse_args()

    try:
        asyncio.run(main(args.api_token))
    except KeyboardInterrupt:
        _LOGGER.info("Program interrupted by user")
