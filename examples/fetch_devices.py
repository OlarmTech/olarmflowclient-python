import asyncio
import logging
import argparse
import sys
from pathlib import Path

# Add parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))

from olarmflowclient import OlarmFlowClient, OlarmFlowClientApiError, DevicesNotFound

# Set up logging
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


async def main(api_token):
    try:
        async with OlarmFlowClient(api_token) as client:
            # Get all devices
            _LOGGER.info("Fetching devices...")
            devices_result = await client.get_devices()

            # check result
            devices = devices_result.get("data", [])
            if len(devices) == 0:
                _LOGGER.error("No devices found")
                return
            _LOGGER.info(f"Found {len(devices)} devices.")

            # log devices
            for index, device in enumerate(devices):
                _LOGGER.info(
                    f"{index + 1}. {device.get('deviceName')} -> {device.get('deviceId')}"
                )

    except DevicesNotFound as e:
        _LOGGER.error(f"No devices found for this account: {e}")
    except OlarmFlowClientApiError as e:
        _LOGGER.error(f"API Error occurred: {e}")
    except Exception as e:
        _LOGGER.exception(f"Unexpected error occurred: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Olarm Flow Client Example - Fetch Devices"
    )
    parser.add_argument("--api-token", required=True, help="Your Olarm API token")
    args = parser.parse_args()

    asyncio.run(main(args.api_token))
