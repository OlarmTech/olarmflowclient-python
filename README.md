# OlarmConnect

## About Olarm

[Olarm](https://www.olarm.com) is a smart security company that transforms existing alarm systems into connected, app-controlled security solutions. Their products work with major alarm panels (Paradox, DSC, Texecom, IDS, Honeywell and more) to provide remote control, real-time alerts, and comprehensive security management from anywhere.

## Python Client

This is the official asynchronous Python client for interacting with the Olarm Public API.
For more information about Olarm, visit [https://www.olarm.com](https://www.olarm.com).

## Features

*   Fetch your devices information and state
*   Send commands to devices (arm, disarm, stay, sleep, bypass zones, control PGM etc..).
*   Subscribe to real-time state changes and events using MQTT

## Quick Start

1. Sign up at https://login.olarm.com
2. Go to API section, generate token
3. `pip install olarmconnect`
4. Copy this code:
```
import asyncio
from olarmconnect import OlarmConnect

async def main():
    async with OlarmConnect("your-token-here") as client:
        devices = await client.get_devices()
        print(f"You have {len(devices['data'])} devices")

# For non-async code:
import asyncio
result = asyncio.run(main())
```

Please check the examples for more uses!

## Examples

The repository includes example scripts that demonstrate how to use the library:

```bash
# Run the fetch devices example
python examples/fetch_devices.py --api-token YOUR_API_TOKEN

# Run the MQTT events listener example
python examples/subscribe_device_mqtt.py --api-token YOUR_API_TOKEN --user-id YOUR_USER_ID
```
NOTE: you can find your User ID in the Olarm user portal

This library provides asynchronous access using `aiohttp` for API calls and `paho-mqtt` for real-time event handling via MQTT.

## Development

1.  Clone the repository.
2.  Setup venv if necessary
    ```bash
    python3 -m venv venv
    source venv/bin/activate # other shells might be different 
    ```
3.  Install dependencies:
    ```bash
    pip3 install -r requirements.txt
    ```
4.  Format code and check for linting errors using Ruff:
    ```bash
    # Check for issues
    python3 -m ruff check .

    # Fix issues and format code
    python3 -m ruff check . --fix
    python3 -m ruff format .
    ```

## Running Tests

The project uses pytest and pytest-asyncio for testing. To run the tests:

1. Make sure you have the testing dependencies installed:
   ```bash
   pip install -r requirements.txt
   ```

2. Run the tests with pytest:
   ```bash
   python -m pytest
   ```

## Issues / Feature Requests

Please log issues and feature requests in Github issues 👆

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.
