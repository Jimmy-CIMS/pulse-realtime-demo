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

    async def receive_json():
        message = await asyncio.wait_for(outgoing.get(), timeout=3)
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
            for path in ["/", "/static/style.css", "/static/app.js", "/docs",
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
