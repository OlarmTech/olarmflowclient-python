"""Tests for OlarmFlowClient MQTT functionality (aiomqtt-based reconnect loop)."""

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import aiomqtt
from aiomqtt.exceptions import MqttConnectError as AiomqttConnectError
import pytest

import olarmflowclient.olarmflowclient as olarm_module
from olarmflowclient import (
    MqttAuthError,
    MqttConnectError,
    MqttTimeoutError,
    OlarmFlowClient,
)


class FakeMessage:
    """Minimal stand-in for aiomqtt.Message."""

    def __init__(self, topic: str, payload: Any) -> None:
        self.topic = topic
        self.payload = payload


class FakeMqttClient:
    """Fake aiomqtt.Client: async context manager + async message iterator."""

    def __init__(self, behavior: Any = "ok", **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.behavior = behavior  # "ok", "hang", or an Exception to raise on connect
        self.subscribed: list[str] = []
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    async def __aenter__(self) -> "FakeMqttClient":
        if self.behavior == "hang":
            await asyncio.Event().wait()
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    def push_message(self, topic: str, payload: Any) -> None:
        self._queue.put_nowait(FakeMessage(topic, payload))

    def push_error(self, error: Exception) -> None:
        self._queue.put_nowait(error)

    @property
    def messages(self) -> Any:
        return self._message_iter()

    async def _message_iter(self) -> Any:
        while True:
            item = await self._queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture
def fake_mqtt(monkeypatch):
    """Patch aiomqtt.Client with a scriptable fake and disable backoff delays.

    Returns a holder with `created` (all fake client instances, in order) and
    `script` (per-attempt behavior: "ok", "hang", or an Exception).
    """

    class Holder:
        created: list[FakeMqttClient] = []
        script: list[Any] = []

    def factory(**kwargs: Any) -> FakeMqttClient:
        behavior = Holder.script.pop(0) if Holder.script else "ok"
        client = FakeMqttClient(behavior, **kwargs)
        Holder.created.append(client)
        return client

    monkeypatch.setattr(olarm_module.aiomqtt, "Client", factory)
    monkeypatch.setattr(olarm_module, "MQTT_RECONNECT_BACKOFF_MIN", 0.0)
    monkeypatch.setattr(olarm_module, "MQTT_RECONNECT_BACKOFF_MAX", 0.0)
    return Holder


@pytest.fixture
def access_token():
    return "test_access_token"


@pytest.fixture
def user_id():
    return "test_user_id"


@pytest.fixture
def device_id():
    return "test_device_id"


async def _settle(steps: int = 10) -> None:
    """Let scheduled tasks and callbacks run."""
    for _ in range(steps):
        await asyncio.sleep(0)


class TestMqtt:
    async def test_start_success_and_message_flow(
        self, fake_mqtt, access_token, user_id, device_id
    ):
        """start_mqtt_async connects, subscribes and routes messages."""
        client = OlarmFlowClient(access_token)
        status_callback = MagicMock()
        client.set_mqtt_status_callback(status_callback)

        await client.start_mqtt_async(user_id, "test_suffix", timeout=5.0)

        fake = fake_mqtt.created[0]
        assert fake.kwargs["identifier"] == f"{user_id}-test_suffix"
        assert fake.kwargs["password"] == access_token
        assert fake.kwargs["transport"] == "websockets"
        assert fake.kwargs["websocket_path"] == "/mqtt"

        # Subscribe after start (as the HA integration does)
        message_callback = MagicMock()
        client.subscribe_to_device(device_id, message_callback)
        await _settle()
        topic = f"v4/devices/{device_id}"
        assert topic in fake.subscribed

        # Deliver a message
        payload = {"event": "test", "data": "value"}
        fake.push_message(topic, json.dumps(payload).encode())
        await _settle()
        message_callback.assert_called_once_with(topic, payload)

        statuses = [call.args[0] for call in status_callback.call_args_list]
        assert statuses == ["connecting", "connected"]

        client.stop_mqtt()
        await _settle()

    async def test_message_invalid_json_ignored(
        self, fake_mqtt, access_token, user_id, device_id
    ):
        """Messages with undecodable payloads don't reach the callback."""
        client = OlarmFlowClient(access_token)
        message_callback = MagicMock()
        client.subscribe_to_device(device_id, message_callback)

        await client.start_mqtt_async(user_id, timeout=5.0)
        fake = fake_mqtt.created[0]
        fake.push_message(f"v4/devices/{device_id}", b"invalid json")
        await _settle()

        message_callback.assert_not_called()
        client.stop_mqtt()
        await _settle()

    @pytest.mark.parametrize("rc", [4, 5, 134, 135])
    async def test_first_connect_auth_refused(
        self, fake_mqtt, access_token, user_id, rc
    ):
        """Auth-refused first CONNACK raises MqttAuthError and stops cleanly."""
        client = OlarmFlowClient(access_token)
        status_callback = MagicMock()
        client.set_mqtt_status_callback(status_callback)
        fake_mqtt.script.append(AiomqttConnectError(rc))

        with pytest.raises(MqttAuthError):
            await client.start_mqtt_async(user_id, timeout=5.0)

        await _settle()
        assert client._mqtt_task is None
        # No stale reconnecting/disconnected statuses after a refused first connect
        statuses = [call.args[0] for call in status_callback.call_args_list]
        assert statuses == ["connecting"]

    async def test_first_connect_refused_non_auth(
        self, fake_mqtt, access_token, user_id
    ):
        """Non-auth refusal raises MqttConnectError, not MqttAuthError."""
        client = OlarmFlowClient(access_token)
        fake_mqtt.script.append(AiomqttConnectError(3))  # server unavailable

        with pytest.raises(MqttConnectError) as exc_info:
            await client.start_mqtt_async(user_id, timeout=5.0)

        assert not isinstance(exc_info.value, MqttAuthError)
        await _settle()
        assert client._mqtt_task is None

    async def test_first_connect_timeout(self, fake_mqtt, access_token, user_id):
        """A hanging first connect raises MqttTimeoutError and cancels the task."""
        client = OlarmFlowClient(access_token)
        fake_mqtt.script.append("hang")

        with pytest.raises(MqttTimeoutError):
            await client.start_mqtt_async(user_id, timeout=0.05)

        await _settle()
        assert client._mqtt_task is None

    async def test_reconnect_after_drop(
        self, fake_mqtt, access_token, user_id, device_id
    ):
        """A dropped connection reconnects and re-subscribes."""
        client = OlarmFlowClient(access_token)
        status_callback = MagicMock()
        client.set_mqtt_status_callback(status_callback)
        client.subscribe_to_device(device_id, MagicMock())

        await client.start_mqtt_async(user_id, timeout=5.0)

        # Drop the established connection
        fake_mqtt.created[0].push_error(aiomqtt.MqttError("Connection lost"))
        await _settle()

        assert len(fake_mqtt.created) == 2
        assert f"v4/devices/{device_id}" in fake_mqtt.created[1].subscribed
        assert client._mqtt_retries == 0

        statuses = [call.args[0] for call in status_callback.call_args_list]
        assert statuses == [
            "connecting",
            "connected",
            "reconnecting",
            "connecting",
            "connected",
        ]
        reconnecting_info = status_callback.call_args_list[2].args[1]
        assert "Connection lost" in reconnecting_info["reason"]
        assert reconnecting_info["rc"] is None
        assert reconnecting_info["auth"] is False

        client.stop_mqtt()
        await _settle()

    async def test_reconnect_auth_refusal_flagged_in_info(
        self, fake_mqtt, access_token, user_id
    ):
        """An auth-refused reconnect reports auth=True and the CONNACK rc."""
        client = OlarmFlowClient(access_token)
        status_callback = MagicMock()
        client.set_mqtt_status_callback(status_callback)

        await client.start_mqtt_async(user_id, timeout=5.0)

        # Drop the connection, then refuse the reconnect with an auth error
        fake_mqtt.script.append(AiomqttConnectError(5))
        fake_mqtt.created[0].push_error(aiomqtt.MqttError("Connection lost"))
        await _settle(steps=30)

        infos = [
            call.args[1]
            for call in status_callback.call_args_list
            if call.args[0] == "reconnecting"
        ]
        assert infos[0]["auth"] is False and infos[0]["rc"] is None
        assert infos[1]["auth"] is True and infos[1]["rc"] == 5

        client.stop_mqtt()
        await _settle()

    async def test_token_rotation_used_on_reconnect(
        self, fake_mqtt, access_token, user_id
    ):
        """A token updated while connected is used on the next reconnect."""
        client = OlarmFlowClient(access_token)
        await client.start_mqtt_async(user_id, timeout=5.0)
        assert fake_mqtt.created[0].kwargs["password"] == access_token

        await client.update_access_token("new_token", 9999999999)
        fake_mqtt.created[0].push_error(aiomqtt.MqttError("Connection lost"))
        await _settle()

        assert fake_mqtt.created[1].kwargs["password"] == "new_token"

        client.stop_mqtt()
        await _settle()

    async def test_disconnected_status_after_retry_threshold(
        self, fake_mqtt, access_token, user_id
    ):
        """'disconnected' is reported once when crossing the retry threshold."""
        client = OlarmFlowClient(access_token, mqtt_retries_before_disconnect=2)
        status_callback = MagicMock()
        client.set_mqtt_status_callback(status_callback)

        await client.start_mqtt_async(user_id, timeout=5.0)

        # Drop the connection (failure 1), fail the next two connects
        # (failures 2 and 3), then recover
        fake_mqtt.script.extend([AiomqttConnectError(3), AiomqttConnectError(3)])
        fake_mqtt.created[0].push_error(aiomqtt.MqttError("Connection lost"))
        await _settle(steps=40)

        statuses = [call.args[0] for call in status_callback.call_args_list]
        assert statuses == [
            "connecting",
            "connected",
            "reconnecting",  # failure 1 (below threshold)
            "connecting",
            "disconnected",  # failure 2 (threshold crossed, reported once)
            "connecting",
            "reconnecting",  # failure 3 (beyond threshold, no repeat)
            "connecting",
            "connected",  # recovery
        ]
        assert client._mqtt_retries == 0

        client.stop_mqtt()
        await _settle()

    async def test_stop_mqtt_idempotent(self, fake_mqtt, access_token, user_id):
        """stop_mqtt cancels the loop task and is safe to call repeatedly."""
        client = OlarmFlowClient(access_token)

        # Safe to call before start
        client.stop_mqtt()

        await client.start_mqtt_async(user_id, timeout=5.0)
        task = client._mqtt_task
        assert task is not None and not task.done()

        client.stop_mqtt()
        await _settle()
        assert task.cancelled()
        assert client._mqtt_task is None
        assert client._mqtt_client is None

        # Safe to call again
        client.stop_mqtt()

    async def test_start_when_already_running_is_noop(
        self, fake_mqtt, access_token, user_id
    ):
        """A second start while running doesn't create another connection."""
        client = OlarmFlowClient(access_token)
        await client.start_mqtt_async(user_id, timeout=5.0)
        await client.start_mqtt_async(user_id, timeout=5.0)

        assert len(fake_mqtt.created) == 1

        client.stop_mqtt()
        await _settle()

    async def test_start_when_running_with_different_params_raises(
        self, fake_mqtt, access_token, user_id
    ):
        """A second start with a different client id raises instead of no-op."""
        client = OlarmFlowClient(access_token)
        await client.start_mqtt_async(user_id, timeout=5.0)

        with pytest.raises(RuntimeError, match="already running"):
            await client.start_mqtt_async("other_user", timeout=5.0)
        with pytest.raises(RuntimeError, match="already running"):
            await client.start_mqtt_async(user_id, "other_suffix", timeout=5.0)

        assert len(fake_mqtt.created) == 1

        client.stop_mqtt()
        await _settle()
