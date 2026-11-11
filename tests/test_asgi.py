"""Protocol-level checks without network sockets or third-party HTTP clients."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.main import create_app


async def request(app, path, method="GET", data=None):
    body = b"" if data is None else json.dumps(data).encode()
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
        "server": ("test", 80), "client": ("test", 123),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, raw


async def json_request(app, path, method="GET", data=None):
    status, body = await request(app, path, method, data)
    return status, json.loads(body)


@asynccontextmanager
async def websocket(app):
    incoming: asyncio.Queue = asyncio.Queue()
    outgoing: asyncio.Queue = asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "root_path": "",
             "query_string": b"", "headers": [], "server": ("test", 80),
             "client": ("test", 123), "subprotocols": []}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))

    async def receive_json(raw=False):
        message = await asyncio.wait_for(outgoing.get(), timeout=3)
        if raw:
            return message
        assert message["type"] == "websocket.send"
        return json.loads(message["text"])

    try:
        message = await asyncio.wait_for(outgoing.get(), timeout=3)
        assert message["type"] == "websocket.accept"
        yield incoming, receive_json
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=3)


def test_http_routes_assets_and_proxy(monkeypatch):
    monkeypatch.setenv("ROOT_PATH", "/proxy/8000")

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            for path in ["/", "/static/style.css", "/static/app.js", "/static/diagnostics.js", "/docs",
                         "/proxy/8000/", "/proxy/8000/static/app.js"]:
                status, raw = await request(app, path)
                assert status == 200
                assert raw
            status, health = await json_request(app, "/api/health")
            assert status == 200
            assert health["storage"] == "memory"
            status, snapshot = await json_request(app, "/api/snapshot")
            assert snapshot["synthetic"] is True
            assert len(snapshot["servers"]) == 6
            assert len(snapshot["history"]) >= 1
            assert (await json_request(app, "/api/servers"))[1] == snapshot["servers"]
            _, schema = await json_request(app, "/openapi.json")
            assert schema["servers"] == [{"url": "/proxy/8000"}]
    asyncio.run(scenario())


@pytest.mark.parametrize("path,method,data,expected", [
    ("/api/settings", "PUT", {"interval": 0}, 422),
    ("/api/settings", "PUT", {"interval": 6}, 422),
    ("/api/settings", "PUT", {"extra": True}, 422),
    ("/api/incidents", "POST", {"server_id": "srv-01", "kind": "invalid"}, 422),
    ("/api/incidents", "POST", {"server_id": "missing", "kind": "cpu"}, 404),
])
def test_invalid_inputs(path, method, data, expected):
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            status, _ = await json_request(app, path, method, data)
            assert status == expected
    asyncio.run(scenario())


def test_shared_websocket_controls_pause_recovery_and_disconnect():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with websocket(app) as (incoming_a, receive_a), websocket(app) as (_, receive_b):
                for receive in [receive_a, receive_b]:
                    initial = await receive()
                    assert initial["type"] == "snapshot"
                    assert len(initial["history"]) >= 1
                status, settings = await json_request(app, "/api/settings", "PUT",
                                                       {"paused": True, "interval": 0.25})
                assert status == 200 and settings["paused"]
                for receive in [receive_a, receive_b]:
                    assert (await receive())["settings"]["paused"]
                sequence = app.state.simulator.sequence
                await incoming_a.put({"type": "websocket.receive", "text": "ping"})
                # The simulator's initial sleep is one second, then emits heartbeats while paused.
                await asyncio.sleep(1.05)
                for receive in [receive_a, receive_b]:
                    heartbeat = await receive()
                    assert heartbeat["sequence"] == sequence
                for kind, expected in [("cpu", "warning"), ("offline", "offline"),
                                       ("recover", "healthy")]:
                    _, result = await json_request(app, "/api/incidents", "POST",
                                                    {"server_id": "srv-01", "kind": kind})
                    for receive in [receive_a, receive_b]:
                        data = await receive()
                        assert data["sequence"] == result["sequence"]
                        assert data["servers"][0]["status"] == expected
                    if kind == "offline":
                        assert result["summary"]["online"] == 5
                    if kind == "cpu":
                        assert result["servers"][0]["cpu"] >= 92
            assert not app.state.simulator.subscribers
            async with websocket(app) as (_, receive):
                reconnected = await receive()
                assert reconnected["type"] == "snapshot"
                assert reconnected["sequence"] == app.state.simulator.sequence
            await json_request(app, "/api/settings", "PUT", {"paused": False, "interval": 0.25})
            before = app.state.simulator.sequence
            await asyncio.sleep(0.3)
            assert app.state.simulator.sequence > before
    asyncio.run(scenario())


def test_api_delay_failure_and_recovery_endpoints():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            status, settings = await json_request(app, "/api/diagnostics/faults", "PUT",
                                                  {"delay_ms": 50, "error_status": 503})
            assert status == 200
            assert settings["faults"]["delay_ms"] == 50
            start = asyncio.get_running_loop().time()
            status, body = await json_request(app, "/api/health")
            assert asyncio.get_running_loop().time() - start >= 0.045
            assert status == 503 and body["detail"] == "Simulated API failure"
            for path in ["/api/snapshot", "/api/servers"]:
                assert (await json_request(app, path))[0] == 503
            assert (await json_request(app, "/api/settings", "PUT", {"paused": True}))[0] == 200
            assert (await json_request(app, "/api/diagnostics"))[0] == 200
            status, cleared = await json_request(app, "/api/diagnostics/faults", "DELETE")
            assert status == 200 and cleared["remaining_seconds"] == 0
            assert (await json_request(app, "/api/health"))[0] == 200
            assert (await json_request(app, "/api/snapshot"))[0] == 200
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    {"delay_ms": -1}, {"delay_ms": 15001}, {"error_status": 404},
    {"duration": 0}, {"duration": 121}, {"unknown": True},
])
def test_transport_fault_validation(data):
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            assert (await json_request(app, "/api/diagnostics/faults", "PUT", data))[0] == 422
    asyncio.run(scenario())


def test_transport_faults_expire_without_manual_recovery():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            await json_request(app, "/api/diagnostics/faults", "PUT",
                               {"pause_stream": True, "error_status": 500, "duration": 1})
            assert (await json_request(app, "/api/health"))[0] == 500
            await asyncio.sleep(1.05)
            assert (await json_request(app, "/api/health"))[0] == 200
            _, status = await json_request(app, "/api/diagnostics")
            assert not status["faults"]["pause_stream"]
            assert status["remaining_seconds"] == 0
    asyncio.run(scenario())


def test_paused_broadcast_keeps_pong_and_recovers_latest_state():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with websocket(app) as (incoming, receive):
                await receive()
                _, settings = await json_request(app, "/api/diagnostics/faults", "PUT",
                                                  {"pause_stream": True})
                assert settings["connected_clients"] == 1
                # Publish new state while the transport is muted; no telemetry is sent.
                sim = app.state.simulator
                sim.tick()
                sim.publish()
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(receive(), timeout=0.05)
                await incoming.put({"type": "websocket.receive",
                                    "text": json.dumps({"type": "ping", "id": "probe-1"})})
                pong = await receive()
                assert pong["type"] == "pong" and pong["id"] == "probe-1"
                await json_request(app, "/api/diagnostics/faults", "DELETE")
                update = await receive()
                assert update["type"] == "update" and update["sequence"] == sim.sequence
            assert not app.state.network.connections
    asyncio.run(scenario())


def test_disconnect_all_sockets_and_accept_reconnections():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with websocket(app) as (_, a), websocket(app) as (_, b):
                await a()
                await b()
                _, diagnostics = await json_request(app, "/api/diagnostics")
                assert diagnostics["connected_clients"] == 2
                status, result = await json_request(app, "/api/diagnostics/disconnect", "POST")
                assert status == 200 and result["disconnected_clients"] == 2
                for receive in [a, b]:
                    close = await receive(raw=True)
                    assert close["type"] == "websocket.close"
                    assert close["code"] == 1012
            assert not app.state.network.connections
            assert not app.state.simulator.subscribers
            async with websocket(app) as (_, receive):
                assert (await receive())["type"] == "snapshot"
    asyncio.run(scenario())
