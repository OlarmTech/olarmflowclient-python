# Re-architecture Option A: Async-native MQTT layer on paho (moderate risk)

## Goal

Refactor the MQTT layer of `olarmflowclient` to be honestly asyncio-native while
keeping paho-mqtt's threaded network loop and built-in auto-reconnect. Delete the
custom retry/status state machine. The Home Assistant consumers then shrink to
thin wrappers. Do NOT change the HTTP/REST portion of the client.

## Repos and key files

| Path | Role |
|---|---|
| `/Users/otto/Olarm/olarmflowclient-python/olarmflowclient/olarmflowclient.py` | The library. HTTP client + MQTT client mixed in one ~986-line class `OlarmFlowClient`. MQTT section is lines ~695–986. |
| `/Users/otto/Olarm/olarmflowclient-python/olarmflowclient/const.py` | `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_KEEPALIVE`, `BASE_URL`. |
| `/Users/otto/Olarm/olarmflowclient-python/tests/test_olarmflowclient.py` | 72 tests. Run: `venv/bin/python -m pytest tests/ -q`. mypy: `venv/bin/python -m mypy olarmflowclient` (1 pre-existing unrelated error at line ~389). |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/mqtt.py` | HACS consumer: `OlarmFlowClientMQTT` wrapper (137 lines). Must be updated to the new API. |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/__init__.py` | Calls `init_mqtt()` during `async_setup_entry`; raises `ConfigEntryNotReady` on `MqttConnectError`/`MqttTimeoutError`. |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/manifest.json` | Pins the `olarmflowclient` version; bump after release. |
| `/Users/otto/Olarm/home-assistant-core/homeassistant/components/olarm/mqtt.py` | Parallel HA-core integration copy (144 lines); mirror the HACS changes there. |

## Current architecture (what exists today)

- `OlarmFlowClient.start_mqtt()` (sync, line ~695): creates `mqtt.Client(client_id=..., transport="websockets")` — paho v1-style constructor. With the pinned `paho-mqtt>=2.1.0` this uses the **deprecated CallbackAPIVersion.VERSION1** (emits `DeprecationWarning`; removed in paho 3.0). Calls `connect_async()` + `loop_start()` (both non-blocking), `reconnect_delay_set(4, 60)`.
- `start_mqtt_async()` (line ~755): runs `start_mqtt` in an executor under `asyncio.wait_for`, then waits for the first CONNACK by blocking a **second executor thread** on a `threading.Event` (`_mqtt_connect_event`). Failure from `_mqtt_on_connect` is stored in `_mqtt_connect_error` and re-raised (`MqttAuthError` for rc 4/5, else `MqttConnectError`; `MqttTimeoutError` on timeout).
- `stop_mqtt()` (line ~818): calls `loop_stop()` (joins the paho thread — **blocking**) + `disconnect()`. Called inline from async code in failure paths.
- Status callback (`set_mqtt_status_callback`): 4 states `"connecting" | "connected" | "disconnected" | "reconnecting"`, delivered via `call_soon_threadsafe` when an event loop is set.
- `_handle_connection_failure()` (line ~864): custom state machine — `_mqtt_retries` counter, `MQTT_RETRIES_BEFORE_DISCONNECT = 3` threshold, rc-code buckets (unrecoverable {1,2,6}, auth {4,5,7}) deciding between "reconnecting" and "disconnected". Duplicates paho's own auto-reconnect responsibility.
- Message callbacks (`_mqtt_on_message`, line ~973): invoked **directly on paho's network thread** (unlike status callbacks). Payloads are JSON-decoded; routed by exact topic match from `_mqtt_callbacks` dict. Subscription: `subscribe_to_device(device_id, cb)` → topic `v4/devices/{device_id}`; topics are (re)subscribed in `_mqtt_on_connect` on every reconnect.
- Token rotation: consumer calls `await client.update_access_token(token, expires_at)`, which calls `username_pw_set()` so paho's next reconnect attempt uses the new password (the access token is the MQTT password; username is `MQTT_USER` const; client id is `{user_id}-{suffix}`).

## Consumer contract today (hacs-olarm/mqtt.py)

- `init_mqtt()`: sets status callback, ensures OAuth token valid, `start_mqtt_async(user_id, client_id_suffix, event_loop=hass.loop, timeout=10)`, then `subscribe_to_device`.
- Status callback: on "connected" deletes an HA repair issue; on "disconnected" logs error, creates repair issue, schedules token refresh; on "reconnecting" schedules token refresh (reactive refresh — fires even for non-auth failures).
- `mqtt_message_callback`: must trampoline to the loop itself via `hass.loop.call_soon_threadsafe` because message callbacks arrive on the paho thread.
- `async_stop()`: wraps `stop_mqtt` in `async_add_executor_job` because it knows it blocks.

## Problems to fix

1. **Deprecated paho callback API** — must pass `mqtt.CallbackAPIVersion.VERSION2` and update callback signatures (`on_connect(client, userdata, flags, reason_code, properties)`, `on_disconnect(client, userdata, flags, reason_code, properties)`). Note: with VERSION2 the rc is a `ReasonCode` object, not an int; the numeric v3.1.1 codes (4 = bad user/password, 5 = not authorised) map to reason codes — auth detection must be rewritten accordingly (e.g. `reason_code == "Bad user name or password"` / `"Not authorized"` or via `reason_code.value` 134/135 vs v3 values; verify against paho 2.1 with MQTTv311 protocol where getReasonCode wraps the v3 rc).
2. **Custom reconnect state machine duplicates paho** — delete `_mqtt_retries`, `MQTT_RETRIES_BEFORE_DISCONNECT`, `_handle_connection_failure`, and the "reconnecting"-vs-"disconnected" threshold logic.
3. **Known bug (unfixed on this branch)**: when the first CONNACK is refused, `_mqtt_on_connect` both stores the error for `start_mqtt_async` *and* schedules a "reconnecting" status callback. The caller gets `MqttAuthError` and the client is stopped, but the last status a consumer saw is "connecting"/"reconnecting" — never "disconnected" — and the stale "reconnecting" can fire after the exception was handled. The new design must make this unrepresentable: during the initial connect, failures surface **only** as the raised exception; steady-state connection changes surface **only** via the status callback.
4. **Wrong async primitives** — `threading.Event` + two executor hops. `start_mqtt` is non-blocking, so no executor/`wait_for` is needed to call it; the CONNACK wait should be an `asyncio.Event` set via `loop.call_soon_threadsafe` from the paho thread and awaited with `asyncio.wait_for`.
5. **Blocking `stop_mqtt` called from async code** — provide `async def stop()` that runs `loop_stop()` in an executor; keep a sync `stop_mqtt()` only if needed for backward compat.
6. **Inconsistent callback delivery** — deliver *all* consumer callbacks (status and messages) on the event loop. Consumers must never need `call_soon_threadsafe`.
7. **God class** — extract the MQTT client into its own class (e.g. `OlarmMqttClient`) in its own module. `OlarmFlowClient` keeps the HTTP API; token updates propagate to the MQTT client (constructor injection of the flow client, or a shared token holder). Keep re-exports in `olarmflowclient/__init__.py` so `from olarmflowclient import OlarmFlowClient, MqttConnectError, ...` keeps working.

## Target design

```python
class OlarmMqttClient:
    def __init__(self, access_token, user_id, client_id_suffix="1", *, loop=None): ...
    async def connect(self, timeout: float = 10.0) -> None
        # raises MqttAuthError / MqttConnectError / MqttTimeoutError;
        # guarantees the paho thread is stopped on failure;
        # emits NO status callbacks for initial-connect failures
    async def stop(self) -> None            # executor-wrapped loop_stop
    def subscribe_to_device(self, device_id, cb) -> None   # cb delivered on the loop
    def set_status_callback(self, cb) -> None
        # cb(status: Literal["connected", "disconnected"], info: dict)
        # info: {"reason": str, "will_reconnect": bool}
        # paho owns retries; "disconnected" with will_reconnect=True replaces "reconnecting"
    def update_access_token(self, token) -> None            # username_pw_set for next reconnect
```

- Two statuses only: `connected` / `disconnected(reason, will_reconnect)`. HA maps
  them 1:1 to repair-issue delete/create; it may trigger token refresh when the
  disconnect reason is auth-related (expose an `is_auth_error: bool` in info).
- Internally: `asyncio.Event` for first CONNACK; store loop at `connect()`;
  every consumer-facing callback goes through one `_dispatch(cb, *args)` helper
  using `call_soon_threadsafe`.
- Keep exception classes and their hierarchy exactly as-is (`MqttAuthError`
  subclass of `MqttConnectError`; all subclass `OlarmFlowClientApiError`).

## Constraints

- Python >= 3.10, `paho-mqtt >= 2.1.0` (already pinned). No new dependencies.
- HTTP client code, error classes, and REST tests must not change.
- Update `tests/test_olarmflowclient.py` MQTT tests to the new API; they mock
  `mqtt.Client`, so also add one unmocked construction test to catch paho API
  breaks (assert no `DeprecationWarning`).
- Update both HA consumers (`hacs-olarm` and `home-assistant-core` copies of
  `custom_components/olarm/mqtt.py` / `components/olarm/mqtt.py`): drop the
  `call_soon_threadsafe` trampoline, drop "connecting"/"reconnecting" branches,
  keep repair-issue logic, refresh token only when `is_auth_error`.
- Bump library version in `pyproject.toml`; bump the requirement in
  `hacs-olarm/custom_components/olarm/manifest.json`.

## Verification

1. `venv/bin/python -m pytest tests/ -q` — all green (in `olarmflowclient-python`).
2. `venv/bin/python -m mypy olarmflowclient` — no new errors (one pre-existing at ~line 389 is unrelated).
3. `venv/bin/python -W error::DeprecationWarning -c "from olarmflowclient import ..."` construct the real paho client — no deprecation warnings.
4. Test scenarios that must hold:
   - Initial connect success → `connect()` returns; status seen: `connected` only.
   - Initial CONNACK refused rc=bad-credentials → `connect()` raises `MqttAuthError`; **zero** status callbacks fired; paho thread stopped.
   - Initial CONNACK never arrives → `MqttTimeoutError`; zero status callbacks; thread stopped.
   - Established connection drops → `disconnected(will_reconnect=True)`, then `connected` on recovery; subscriptions re-established.
   - `stop()` never blocks the event loop (loop_stop runs in executor).
