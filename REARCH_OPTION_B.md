# Re-architecture Option B: Explicit async reconnect loop on aiomqtt (preferred, bigger change)

## Goal

Replace the threaded paho setup in `olarmflowclient` with an asyncio-native MQTT
client (`aiomqtt`) driven by one explicit reconnect loop. This inverts control:
the OAuth token is refreshed *proactively before each connect attempt* instead of
reactively from status callbacks, which deletes the entire status/retry state
machine and its bug class. No background thread, no cross-thread events, no
executor hops. Do NOT change the HTTP/REST portion of the client.

## Repos and key files

| Path | Role |
|---|---|
| `/Users/otto/Olarm/olarmflowclient-python/olarmflowclient/olarmflowclient.py` | The library. HTTP client + MQTT client mixed in one ~986-line class `OlarmFlowClient`. MQTT section is lines ~695–986 — this is what gets replaced. |
| `/Users/otto/Olarm/olarmflowclient-python/olarmflowclient/const.py` | `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_KEEPALIVE`, `BASE_URL`. Broker is reached over **websockets, path `/mqtt`, TLS**; MQTT password = the OAuth access token; username = `MQTT_USER`; client id = `{user_id}-{client_id_suffix}`. |
| `/Users/otto/Olarm/olarmflowclient-python/tests/test_olarmflowclient.py` | 72 tests. Run: `venv/bin/python -m pytest tests/ -q`. mypy: `venv/bin/python -m mypy olarmflowclient` (1 pre-existing unrelated error at ~line 389). |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/mqtt.py` | HACS consumer `OlarmFlowClientMQTT` (137 lines) — becomes a thin adapter. |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/__init__.py` | `async_setup_entry` calls `init_mqtt()`; raises `ConfigEntryNotReady` on `MqttConnectError`/`MqttTimeoutError`; `async_unload_entry` calls `async_stop()`. |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/coordinator.py` | `OlarmDataUpdateCoordinator` — has `async_ensure_token_valid()` (OAuth refresh) and `async_update_from_mqtt(payload)` (must be called on the HA loop). |
| `/Users/otto/Olarm/hacs-olarm/custom_components/olarm/manifest.json` | Pins the `olarmflowclient` version; bump after release. |
| `/Users/otto/Olarm/home-assistant-core/homeassistant/components/olarm/` | Parallel HA-core integration copy; mirror the HACS changes there. |

## What exists today (being replaced)

- Threaded paho (`connect_async` + `loop_start`) with the **deprecated
  CallbackAPIVersion.VERSION1** despite pinning `paho-mqtt>=2.1.0`.
- `start_mqtt_async()`: executor + `threading.Event` bridge to await the first
  CONNACK; raises `MqttAuthError` (CONNACK rc 4/5), `MqttConnectError`, or
  `MqttTimeoutError`. `stop_mqtt()` blocks (`loop_stop()` joins the thread).
- A four-state status callback (`connecting/connected/disconnected/reconnecting`)
  driven by a custom retry counter (`_mqtt_retries`, threshold 3) and rc-code
  buckets in `_handle_connection_failure()` — duplicating paho auto-reconnect.
- Known bug (unfixed here): on a refused first CONNACK the caller gets the
  exception, but a stale "reconnecting" status also fires and no final
  "disconnected" is ever emitted — consumers' state machines are left stranded.
- Message callbacks fire on the paho thread (consumers must
  `call_soon_threadsafe` themselves); status callbacks fire on the loop.
  Subscription topic: `v4/devices/{device_id}`; payloads are JSON dicts;
  topics must be re-subscribed after every reconnect.
- Token rotation today is *reactive*: HA's status callback schedules an OAuth
  refresh on "reconnecting"/"disconnected", then `update_access_token()` calls
  `username_pw_set()` hoping it lands before paho's next retry.

## Target design

New module, e.g. `olarmflowclient/mqtt.py`, exporting `OlarmMqttClient`.
`OlarmFlowClient` keeps only the HTTP API. Keep re-exports in
`olarmflowclient/__init__.py` and keep the exception hierarchy exactly as-is
(`MqttAuthError` ⊂ `MqttConnectError`; `MqttTimeoutError`; all ⊂
`OlarmFlowClientApiError`).

```python
class OlarmMqttClient:
    def __init__(
        self,
        user_id: str,
        client_id_suffix: str = "1",
        *,
        token_provider: Callable[[], Awaitable[str]],   # returns a fresh access token
        on_message: Callable[[str, dict], None],         # called on the event loop
        on_connection_change: Callable[[bool, str | None], None] | None = None,
        # (connected: bool, reason: str | None) — the ONLY state signal
    ) -> None: ...

    async def start(self, device_id: str, timeout: float = 10.0) -> None:
        # Performs the FIRST connect + subscribe inline and raises
        # MqttAuthError / MqttConnectError / MqttTimeoutError on failure
        # (so HA setup can raise ConfigEntryNotReady). On success, spawns
        # the reconnect-loop task and returns.

    async def stop(self) -> None:
        # Cancels the task, awaits it, disconnects cleanly. Never blocks the loop.
```

The reconnect loop (the whole architecture, ~40 lines):

```python
while True:
    token = await self._token_provider()          # proactive refresh, every attempt
    try:
        async with aiomqtt.Client(
            hostname=MQTT_HOST, port=MQTT_PORT,
            username=MQTT_USER, password=token,
            identifier=f"{user_id}-{suffix}",
            transport="websockets", websocket_path="/mqtt",
            tls_context=ssl.create_default_context(),
            keepalive=MQTT_KEEPALIVE,
        ) as client:
            await client.subscribe(f"v4/devices/{device_id}")
            self._notify(True, None)
            async for message in client.messages:
                self._dispatch(message)            # json.loads + on_message
    except aiomqtt.MqttError as err:
        self._notify(False, str(err))
        await asyncio.sleep(backoff())             # e.g. 4s → 60s, like today's reconnect_delay_set(4, 60)
    except asyncio.CancelledError:
        raise
```

Notes:

- `start()` runs the first iteration's connect+subscribe with
  `asyncio.wait_for(..., timeout)` before handing off to the background task, and
  maps failures: aiomqtt auth-refused CONNACK (`MqttCodeError` with bad
  user/password or not-authorized reason) → `MqttAuthError`; other `MqttError` →
  `MqttConnectError`; timeout → `MqttTimeoutError`. After `start()` returns,
  failures are non-raising: the loop retries forever and reports via
  `on_connection_change` only.
- No `update_access_token()` on the MQTT client — obsolete. The token_provider
  is called before every attempt; in HA it is
  `coordinator.async_ensure_token_valid()` + return the session token.
- Everything runs on the event loop; callbacks need no thread marshalling.

## Consumer rewrite (hacs-olarm/custom_components/olarm/mqtt.py)

Shrinks to roughly: construct `OlarmMqttClient` with
`token_provider=self._get_fresh_token`, `on_message=coordinator-forward`
(direct call, no `call_soon_threadsafe`), and `on_connection_change` that
creates/deletes the HA repair issue (`ir.async_create_issue` /
`ir.async_delete_issue`, key `mqtt_disconnected_{device_id}`,
translation_key `mqtt_disconnected`, placeholder `reason`). `init_mqtt()` =
`await client.start(device_id, timeout=10)`. `async_stop()` = `await
client.stop()`. In HA, prefer holding the loop task via
`entry.async_create_background_task` semantics if the wrapper spawns it itself —
otherwise let the library own the task, which is the intended design.

## Dependencies and constraints

- Add `aiomqtt>=2.4` to `pyproject.toml` / `requirements.txt` (aiomqtt 2.x wraps
  paho 2.x; keep `paho-mqtt>=2.1.0` as its transitive dep). Verify the installed
  aiomqtt version's exact kwarg names (`identifier`, `websocket_path`,
  `tls_context`) against its docs before coding.
- Python >= 3.10. HTTP client code, error classes, and REST tests unchanged.
- Rewrite the MQTT tests: drop the `mqtt.Client` mocks; mock/fake
  `aiomqtt.Client` (async context manager + async message iterator). Cover the
  scenarios below.
- Update both HA consumers (hacs-olarm and the home-assistant-core copy). Bump
  library version in `pyproject.toml` and the pin in
  `hacs-olarm/custom_components/olarm/manifest.json`.

## Verification

1. `venv/bin/python -m pytest tests/ -q` (in `olarmflowclient-python`) — all green.
2. `venv/bin/python -m mypy olarmflowclient` — no new errors.
3. Scenarios that must hold:
   - `start()` success → returns; `on_connection_change(True, None)` fired once; messages flow to `on_message` on the loop.
   - First connect refused (auth) → `start()` raises `MqttAuthError`; no loop task left running; no connection-change callbacks fired.
   - First connect timeout → `MqttTimeoutError`; same cleanup guarantees.
   - Established connection drops → `on_connection_change(False, reason)`, token_provider called again, reconnect with backoff, `on_connection_change(True, None)` on recovery, subscription re-established.
   - Token expiry while disconnected → next attempt uses the *new* token (assert token_provider return value reaches the aiomqtt Client password).
   - `stop()` → task cancelled and awaited; idempotent; never blocks the loop.
