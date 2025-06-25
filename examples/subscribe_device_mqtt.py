import asyncio
import logging
import argparse
import sys
from pathlib import Path
import ssl

# Add parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))

from olarmconnect import OlarmConnect, OlarmConnectApiError

# Set up logging
logging.basicConfig(level=logging.DEBUG)
_LOGGER = logging.getLogger(__name__)


async def main(api_token):
    try:
        async with OlarmConnect(api_token) as client:
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

            # Connect to MQTT broker (using client_id_suffix="5" to avoid conflicts if user is already setup a client)
            client.start_mqtt(
                user_id, ssl_context=ssl.create_default_context(), client_id_suffix="5"
            )

            # wait a few seconds to give the client time to connect
            await asyncio.sleep(3)

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

    except OlarmConnectApiError as e:
        _LOGGER.error(f"API Error occurred: {e}")
    except Exception as e:
        _LOGGER.exception(f"Unexpected error occurred: {e}")


# Define callback function for MQTT messages
def message_callback(topic, payload):
    _LOGGER.info("Received MQTT message:")
    _LOGGER.info(f"Topic: {topic}")
    _LOGGER.info(f"Payload: {payload}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Olarm Connect API")
    parser.add_argument(
        "--api-token", required=True, help="Your Olarm Connect API token"
    )

    args = parser.parse_args()

    try:
        asyncio.run(main(args.api_token))
    except KeyboardInterrupt:
        _LOGGER.info("Program interrupted by user")
