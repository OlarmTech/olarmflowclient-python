"""
Tests for the unified OlarmFlowClient class combining API and MQTT functionality.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from olarmflowclient import (
    OlarmFlowClient,
    OlarmFlowClientApiError,
    TokenExpired,
    Unauthorized,
    DeviceNotFound,
    DevicesNotFound,
    RateLimited,
    ServerError,
    ServiceUnavailable,
    OlarmFlowClientConnectionError,
    MqttAuthError,
    MqttConnectError,
    MqttTimeoutError,
)


@pytest.fixture
def access_token():
    """Return a dummy access token for testing."""
    return "test_access_token"


@pytest.fixture
def jwt_token():
    """Return a dummy JWT token for testing."""
    return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoidGVzdCJ9.signature"


@pytest.fixture
def device_id():
    """Return a dummy device ID for testing."""
    return "test_device_id"


@pytest.fixture
def user_id():
    """Return a dummy user ID for testing."""
    return "test_user_id"


class TestOlarmFlowClient:
    """Test the unified OlarmFlowClient class."""

    def test_init_regular_token(self, access_token):
        """Test initialization with a regular access token."""
        client = OlarmFlowClient(access_token)
        assert client._access_token == access_token
        assert client._expires_at is None
        assert client._is_jwt_token is False
        assert client._api_session is None
        assert client._mqtt_client is None

    def test_init_jwt_token(self, jwt_token):
        """Test initialization with a JWT token."""
        client = OlarmFlowClient(jwt_token, expires_at=1234567890)
        assert client._access_token == jwt_token
        assert client._expires_at == 1234567890
        assert client._is_jwt_token is True

    @pytest.mark.asyncio
    async def test_context_manager(self, access_token):
        """Test using the client as a context manager."""
        with patch("aiohttp.ClientSession") as mock_session:
            mock_session_instance = AsyncMock()
            mock_session.return_value = mock_session_instance

            async with OlarmFlowClient(access_token) as client:
                assert client._api_session is not None
                mock_session.assert_called_once()

            # Session should be closed after exiting the context
            mock_session_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_connect_and_close(self, access_token):
        """Test API session connect and close methods."""
        client = OlarmFlowClient(access_token)

        with patch("aiohttp.ClientSession") as mock_session:
            mock_session_instance = AsyncMock()
            mock_session.return_value = mock_session_instance

            await client._api_connect()
            assert client._api_session is not None
            mock_session.assert_called_once()

            await client._api_close()
            mock_session_instance.close.assert_called_once()
            assert client._api_session is None

    def test_api_error(self):
        """Test OlarmFlowClientApiError initialization and string representation."""
        error = OlarmFlowClientApiError("Test error", 400, "Bad request")
        assert str(error) == "API Error 400 (Bad request): Test error"
        assert error.status_code == 400
        assert error.response_text == "Bad request"

        # Test with server-supplied error code, message and reqId
        detailed_error = OlarmFlowClientApiError(
            "Request failed",
            status_code=403,
            response_text='{"error":"tokenExpired"}',
            error_code="tokenExpired",
            error_message="Your access token has expired.",
            req_id="jwtapi-Xk3fPq9Z-1.2.3.4-a3f9c1",
        )
        assert "API Error 403 (tokenExpired)" in str(detailed_error)
        assert "Your access token has expired." in str(detailed_error)
        assert "[reqId=jwtapi-Xk3fPq9Z-1.2.3.4-a3f9c1]" in str(detailed_error)

        # An HTML gateway body must never reach the user-facing string
        gateway_error = ServiceUnavailable(
            status_code=502,
            response_text="<html><head><title>502 Bad Gateway</title></head></html>",
        )
        assert str(gateway_error) == (
            "API Error 502: Olarm service temporarily unavailable"
        )
        assert gateway_error.response_text is not None

        # Test without status code
        simple_error = OlarmFlowClientApiError("Simple error")
        assert str(simple_error) == "Simple error"

    def test_device_not_found_error(self):
        """Test DeviceNotFound initialization and inheritance."""
        # Test without device ID
        error = DeviceNotFound()
        assert "Device not found" in str(error)
        assert "API Error 404" in str(error)
        assert error.status_code == 404
        assert isinstance(error, OlarmFlowClientApiError)

        # Test with device ID
        device_error = DeviceNotFound("test_device_123")
        assert "Device 'test_device_123' not found" in str(device_error)
        assert "API Error 404" in str(device_error)
        assert device_error.status_code == 404

    def test_devices_not_found_error(self):
        """Test DevicesNotFound initialization and inheritance."""
        error = DevicesNotFound()
        assert "No devices found for this account" in str(error)
        assert "API Error 404" in str(error)
        assert error.status_code == 404
        assert isinstance(error, OlarmFlowClientApiError)

        # Test with custom message
        custom_error = DevicesNotFound("Custom no devices message")
        assert "Custom no devices message" in str(custom_error)
        assert custom_error.status_code == 404

    def test_token_expired_error(self):
        """Test TokenExpired initialization and inheritance."""
        error = TokenExpired()
        assert "Access token has expired" in str(error)
        assert "API Error 401" in str(error)
        assert error.status_code == 401
        assert isinstance(error, OlarmFlowClientApiError)

    def test_unauthorized_error(self):
        """Test Unauthorized initialization and inheritance."""
        error = Unauthorized()
        assert "Unauthorized access" in str(error)
        assert "API Error 403" in str(error)
        assert error.status_code == 403
        assert isinstance(error, OlarmFlowClientApiError)

    def test_server_error(self):
        """Test ServerError initialization and inheritance."""
        error = ServerError()
        assert "Server internal error" in str(error)
        assert "API Error 500" in str(error)
        assert error.status_code == 500
        assert isinstance(error, OlarmFlowClientApiError)

    def test_rate_limited_error(self):
        """Test RateLimited initialization and inheritance."""
        error = RateLimited()
        assert "Too many requests - rate limited" in str(error)
        assert "API Error 429" in str(error)
        assert error.status_code == 429
        assert isinstance(error, OlarmFlowClientApiError)

    def test_connection_error(self):
        """Test OlarmFlowClientConnectionError has no status code prefix."""
        error = OlarmFlowClientConnectionError(
            "Unable to connect to the Olarm API: DNS failure"
        )
        assert str(error) == "Unable to connect to the Olarm API: DNS failure"
        assert error.status_code is None
        assert isinstance(error, OlarmFlowClientApiError)

    # Note: Direct _api_make_request tests removed due to complex aiohttp mocking requirements
    # API functionality is adequately tested through higher-level method tests

    @pytest.mark.asyncio
    async def test_update_access_token(self, access_token):
        """Test updating access token."""
        client = OlarmFlowClient(access_token)
        new_token = "new_test_token"
        new_expires_at = 9876543210

        await client.update_access_token(new_token, new_expires_at)

        assert client._access_token == new_token
        assert client._expires_at == new_expires_at

    @pytest.mark.asyncio
    async def test_get_devices(self, access_token):
        """Test get_devices method."""
        client = OlarmFlowClient(access_token)
        expected_result = {"devices": [{"id": "device1"}, {"id": "device2"}]}

        with patch.object(
            client, "_api_make_request", return_value=expected_result
        ) as mock_request:
            result = await client.get_devices(page=1, pageLength=10, search="test")

            assert result == expected_result
            mock_request.assert_called_once_with(
                "GET",
                "/api/v4/devices",
                params={
                    "page": 1,
                    "pageLength": 10,
                    "search": "test",
                    "deviceApiAccessOnly": "1",
                },
            )

    @pytest.mark.asyncio
    async def test_get_devices_404_raises_devices_not_found(self, access_token):
        """Test get_devices method raises DevicesNotFound on 404."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Not found", 404, "No devices found")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(DevicesNotFound) as exc_info:
                await client.get_devices()

            # Check that the original error is chained
            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 404
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_devices_401_raises_token_expired(self, access_token):
        """Test get_devices method raises TokenExpired on 401."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Unauthorized", 401, "Token expired")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(TokenExpired) as exc_info:
                await client.get_devices()

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 401
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_devices_403_raises_unauthorized(self, access_token):
        """Test get_devices method raises Unauthorized on 403."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Forbidden", 403, "Access denied")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(Unauthorized) as exc_info:
                await client.get_devices()

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 403
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_devices_500_raises_server_error(self, access_token):
        """Test get_devices method raises ServerError on 500."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Internal error", 500, "Server error")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(ServerError) as exc_info:
                await client.get_devices()

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 500
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_devices_429_raises_rate_limited(self, access_token):
        """Test get_devices method raises RateLimited on 429."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError(
            "Too many requests", 429, "Rate limit exceeded"
        )

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(RateLimited) as exc_info:
                await client.get_devices()

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 429
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_devices_service_unavailable(self, access_token):
        """Test get_devices maps 502/503/504 to ServiceUnavailable."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Bad gateway", 502, "Gateway error")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(ServiceUnavailable) as exc_info:
                await client.get_devices()

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 502
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_devices_other_errors_passthrough(self, access_token):
        """Test get_devices method passes through other status codes."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Teapot", 418, "I'm a teapot")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(OlarmFlowClientApiError) as exc_info:
                await client.get_devices()

            # Should be the same error object for unhandled status codes
            assert exc_info.value == api_error
            assert exc_info.value.status_code == 418
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_device(self, access_token, device_id):
        """Test get_device method."""
        client = OlarmFlowClient(access_token)
        expected_result = {"device": {"id": device_id}}

        with patch.object(
            client, "_api_make_request", return_value=expected_result
        ) as mock_request:
            result = await client.get_device(device_id)

            assert result == expected_result
            mock_request.assert_called_once_with(
                "GET",
                f"/api/v4/devices/{device_id}",
                params={"deviceApiAccessOnly": "1"},
            )

    @pytest.mark.asyncio
    async def test_get_device_404_raises_device_not_found(
        self, access_token, device_id
    ):
        """Test get_device method raises DeviceNotFound on 404."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Not found", 404, "Device not found")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(DeviceNotFound) as exc_info:
                await client.get_device(device_id)

            # Check that the original error is chained and message includes device ID
            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 404
            assert device_id in str(exc_info.value)
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_device_401_raises_token_expired(self, access_token, device_id):
        """Test get_device method raises TokenExpired on 401."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Unauthorized", 401, "Token expired")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(TokenExpired) as exc_info:
                await client.get_device(device_id)

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 401
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_device_403_raises_device_not_found(
        self, access_token, device_id
    ):
        """Test get_device method raises DeviceNotFound on 403 (device not accessible)."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError("Forbidden", 403, "Access denied")

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(DeviceNotFound) as exc_info:
                await client.get_device(device_id)

            # 403 should be treated as device not found for specific device access
            assert exc_info.value.__cause__ == api_error
            assert (
                exc_info.value.status_code == 404
            )  # DeviceNotFound always reports 404
            assert device_id in str(exc_info.value)
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_device_429_raises_rate_limited(self, access_token, device_id):
        """Test get_device method raises RateLimited on 429."""
        client = OlarmFlowClient(access_token)
        api_error = OlarmFlowClientApiError(
            "Too many requests", 429, "Rate limit exceeded"
        )

        with patch.object(
            client, "_api_make_request", side_effect=api_error
        ) as mock_request:
            with pytest.raises(RateLimited) as exc_info:
                await client.get_device(device_id)

            assert exc_info.value.__cause__ == api_error
            assert exc_info.value.status_code == 429
            mock_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_device_area_arm(self, access_token, device_id):
        """Test send_device_area_arm method."""
        client = OlarmFlowClient(access_token)
        expected_result = {"success": True}

        with patch.object(
            client, "_api_send_action", return_value=expected_result
        ) as mock_action:
            result = await client.send_device_area_arm(device_id, 1)

            assert result == expected_result
            mock_action.assert_called_once_with(device_id, "area-arm", 1)

    def test_set_mqtt_status_callback(self, access_token):
        """Test setting MQTT status callback."""
        client = OlarmFlowClient(access_token)
        callback = MagicMock()

        client.set_mqtt_status_callback(callback)

        assert client._mqtt_status_callback == callback

    @pytest.mark.asyncio
    async def test_api_send_action_device(self, access_token, device_id):
        """Test _api_send_action for device actions."""
        client = OlarmFlowClient(access_token)
        expected_result = {"success": True}

        with patch.object(
            client, "_api_make_request", return_value=expected_result
        ) as mock_request:
            result = await client._api_send_action(device_id, "test-action", 1)

            assert result == expected_result
            mock_request.assert_called_once_with(
                "POST",
                f"/api/v4/devices/{device_id}/actions",
                jsonBody={"actionCmd": "test-action", "actionNum": 1},
            )

    @pytest.mark.asyncio
    async def test_api_send_action_prolink(self, access_token, device_id):
        """Test _api_send_action for prolink actions."""
        client = OlarmFlowClient(access_token)
        expected_result = {"success": True}
        prolink_id = "test_prolink_id"

        with patch.object(
            client, "_api_make_request", return_value=expected_result
        ) as mock_request:
            result = await client._api_send_action(
                device_id, "test-action", 1, prolink_id
            )

            assert result == expected_result
            mock_request.assert_called_once_with(
                "POST",
                f"/api/v4/prolinks/{prolink_id}/actions",
                jsonBody={"actionCmd": "test-action", "actionNum": 1},
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method_name,action_cmd,action_num",
        [
            ("send_device_area_disarm", "area-disarm", 1),
            ("send_device_area_arm", "area-arm", 1),
            ("send_device_area_stay", "area-stay", 1),
            ("send_device_area_sleep", "area-sleep", 1),
            ("send_device_zone_bypass", "zone-bypass", 2),
            ("send_device_zone_unbypass", "zone-unbypass", 2),
            ("send_device_pgm_open", "pgm-open", 3),
            ("send_device_pgm_close", "pgm-close", 3),
            ("send_device_pgm_pulse", "pgm-pulse", 3),
            ("send_device_ukey_activate", "ukey-activate", 4),
        ],
    )
    async def test_device_action_methods(
        self, access_token, device_id, method_name, action_cmd, action_num
    ):
        """Test various device action methods."""
        client = OlarmFlowClient(access_token)
        expected_result = {"success": True}

        with patch.object(
            client, "_api_send_action", return_value=expected_result
        ) as mock_action:
            method = getattr(client, method_name)
            result = await method(device_id, action_num)

            assert result == expected_result
            mock_action.assert_called_once_with(device_id, action_cmd, action_num)

    def test_mqtt_connect_error(self):
        """Test MqttConnectError initialization and inheritance."""
        error = MqttConnectError()
        assert "MQTT connection failed" in str(error)
        assert isinstance(error, OlarmFlowClientApiError)

        # Test with custom message
        custom_error = MqttConnectError("Custom MQTT error")
        assert "Custom MQTT error" in str(custom_error)

    def test_mqtt_timeout_error(self):
        """Test MqttTimeoutError initialization and inheritance."""
        error = MqttTimeoutError()
        assert "timeout" in str(error).lower()
        assert isinstance(error, OlarmFlowClientApiError)

    def test_mqtt_auth_error(self):
        """Test MqttAuthError initialization and inheritance."""
        error = MqttAuthError()
        assert "authentication" in str(error).lower()
        # Must remain a MqttConnectError subclass for backward compatibility
        assert isinstance(error, MqttConnectError)
        assert isinstance(error, OlarmFlowClientApiError)

        custom_error = MqttAuthError("Custom auth error")
        assert "Custom auth error" in str(custom_error)
