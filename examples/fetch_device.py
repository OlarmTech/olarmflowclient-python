import asyncio
import logging
import argparse
import sys
from pathlib import Path
import json

# Add parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))

from olarmflowclient import OlarmFlowClient, OlarmFlowClientApiError

# Set up logging
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


async def main(device_id, api_token):
    try:
        async with OlarmFlowClient(api_token) as client:
            # fetch device
            device = await client.get_device(device_id)
            _LOGGER.info(f"Device Info: {json.dumps(device, indent=4)}")

    except OlarmFlowClientApiError as e:
        _LOGGER.error(f"API Error occurred: {e}")
    except Exception as e:
        _LOGGER.exception(f"Unexpected error occurred: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Olarm Flow Client Example - Fetch Device"
    )
    parser.add_argument("--api-token", required=True, help="Your Olarm API token")
    parser.add_argument("--device-id", required=True, help="ID of the device")

    args = parser.parse_args()

    _LOGGER.info(f"Device ID: {args.device_id}")

    asyncio.run(main(args.device_id, args.api_token))
