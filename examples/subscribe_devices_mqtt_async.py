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


async def main(api_token):
    try:
        async with OlarmFlowClient(api_token) as client:
            # Define unified connection status callback
            def on_connection_status(status: str, info: dict) -> None:
                if status == "connected":
                    _LOGGER.info("MQTT: Connected!")
                elif status == "disconnected":
                    _LOGGER.info("MQTT: Disconnected")
                elif status == "reconnecting":
                    _LOGGER.info("MQTT: Reconnecting..")
                elif status == "connecting":
                    _LOGGER.info("MQTT: Connecting..")

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

            # Set up unified MQTT status callback before connecting
            _LOGGER.info("Setting up MQTT status callback...")
            client.set_mqtt_status_callback(on_connection_status)

            # Connect to MQTT broker using async method
            # (use a specific client_id_suffix to avoid conflicts if another client is running)
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
