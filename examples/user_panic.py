import asyncio
import logging
import argparse
import sys
from pathlib import Path

# Add parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))

from olarmflowclient import OlarmFlowClient, OlarmFlowClientApiError

# Set up logging
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


async def main(device_id, api_token):
    try:
        async with OlarmFlowClient(api_token) as client:
            # send user panic action
            _LOGGER.info("Sending User Panic...")
            await client.send_user_panic(device_id)

    except OlarmFlowClientApiError as e:
        _LOGGER.error(f"API Error occurred: {e}")
    except Exception as e:
        _LOGGER.exception(f"Unexpected error occurred: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Olarm Flow Client Example - User Panic"
    )
    parser.add_argument("--api-token", required=True, help="Your Olarm API token")
    parser.add_argument("--device-id", required=True, help="ID of the device")
    args = parser.parse_args()

    _LOGGER.info(f"Device ID: {args.device_id}")

    asyncio.run(main(args.device_id, args.api_token))
