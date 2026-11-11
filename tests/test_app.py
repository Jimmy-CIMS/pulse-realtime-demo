"""Exercise real HTTP and WebSocket connections against an ephemeral Uvicorn server."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from websocket import create_connection

from app.simulation import Incident, Settings, Simulator


@pytest.fixture(scope="module")
def server():
    try:
        probe_socket = socket.socket()
    except PermissionError:
        pytest.skip("Environment forbids network sockets; run ASGI tests here instead")
    with probe_socket as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, ROOT_PATH="/proxy/test")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=Path(__file__).resolve().parents[1], env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("Uvicorn stopped before becoming ready")
            try:
                with urlopen(base + "/api/health", timeout=0.2):
                    break
            except (URLError, TimeoutError):
                time.sleep(0.05)
        else:
            pytest.fail("Uvicorn did not start")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def api(server, path, method="GET", data=None):
    request = Request(server + path, method=method,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=3) as response:
        return json.load(response)


def test_health_assets_and_proxy_prefix(server):
    assert api(server, "/api/health") == {
        "status": "ok", "synthetic": True, "storage": "memory"
    }
    for path in ["/", "/static/app.js", "/static/style.css", "/docs", "/proxy/test/"]:
        with urlopen(server + path) as response:
            assert response.status == 200
    with urlopen(server + "/") as response:
        html = response.read().decode()
    assert 'href="static/style.css"' in html
    assert 'src="static/app.js"' in html
    schema = api(server, "/openapi.json")
    assert schema["servers"] == [{"url": "/proxy/test"}]
    assert len(api(server, "/api/servers")) == 6


@pytest.mark.parametrize("path,method,data,status", [
    ("/api/settings", "PUT", {"interval": 0}, 422),
    ("/api/settings", "PUT", {"interval": 6}, 422),
    ("/api/settings", "PUT", {"unknown": True}, 422),
    ("/api/incidents", "POST", {"server_id": "srv-01", "kind": "invalid"}, 422),
    ("/api/incidents", "POST", {"server_id": "missing", "kind": "cpu"}, 404),
])
def test_validation(server, path, method, data, status):
    with pytest.raises(HTTPError) as exc:
        api(server, path, method, data)
    assert exc.value.code == status


def test_pause_and_fault_recovery(server):
    api(server, "/api/settings", "PUT", {"paused": True, "interval": 0.25})
    before = api(server, "/api/snapshot")
    time.sleep(1.1)
    assert api(server, "/api/snapshot")["sequence"] == before["sequence"]
    for kind, expected in [("cpu", "warning"), ("offline", "offline"), ("recover", "healthy")]:
        state = api(server, "/api/incidents", "POST", {"server_id": "srv-01", "kind": kind})
        assert state["servers"][0]["status"] == expected
        if kind == "cpu":
            assert state["servers"][0]["cpu"] >= 92
        if kind == "offline":
            assert state["servers"][0]["network"] == 0
            assert state["summary"]["online"] == 5
    api(server, "/api/settings", "PUT", {"paused": False, "interval": 0.25})
    seq = api(server, "/api/snapshot")["sequence"]
    time.sleep(0.6)
    assert api(server, "/api/snapshot")["sequence"] > seq


def receive_until(ws, predicate):
    for _ in range(30):
        message = json.loads(ws.recv())
        if predicate(message):
            return message
    pytest.fail("Expected WebSocket message did not arrive")


def test_multiple_websocket_clients_and_reconnect(server):
    ws_url = server.replace("http:", "ws:") + "/ws"
    a = create_connection(ws_url, timeout=3, http_proxy_host=None)
    b = create_connection(ws_url, timeout=3, http_proxy_host=None)
    try:
        for ws in [a, b]:
            initial = json.loads(ws.recv())
            assert initial["type"] == "snapshot"
            assert initial["synthetic"] is True
            assert len(initial["history"]) >= 1
        api(server, "/api/settings", "PUT", {"paused": True, "interval": 0.25})
        result = api(server, "/api/incidents", "POST", {"server_id": "srv-02", "kind": "offline"})
        for ws in [a, b]:
            data = receive_until(ws, lambda d: d["sequence"] >= result["sequence"])
            assert data["servers"][1]["status"] == "offline"
            assert data["settings"]["paused"] is True
        a.close()
        c = create_connection(ws_url, timeout=3, http_proxy_host=None)
        try:
            data = json.loads(c.recv())
            assert data["type"] == "snapshot"
            assert data["servers"][1]["status"] == "offline"
        finally:
            c.close()
        b.send("ping")
        assert json.loads(b.recv())["sequence"] == result["sequence"]
    finally:
        a.close()
        b.close()
        api(server, "/api/incidents", "POST", {"server_id": "srv-02", "kind": "recover"})
        api(server, "/api/settings", "PUT", {"paused": False, "interval": 1})


def test_history_events_and_slow_client_are_bounded():
    import asyncio
    sim = Simulator(seed=42)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    sim.subscribers.add(queue)
    for _ in range(1300):
        sim.tick()
        sim.publish()
    assert len(sim.history) <= 1201
    assert queue.qsize() == 1
    assert queue.get_nowait()["sequence"] == sim.sequence
    for _ in range(100):
        sim.inject(Incident(server_id="srv-01", kind="cpu"))
    assert len(sim.events) == 60
    assert all(0 <= s["cpu"] <= 100 for s in sim.servers)
    assert Simulator(seed=42).snapshot()["sequence"] == 1


def test_new_simulator_resets_state():
    sim = Simulator(seed=1)
    sim.settings = Settings(paused=True)
    sim.inject(Incident(server_id="srv-01", kind="offline"))
    fresh = Simulator(seed=1)
    assert not fresh.settings.paused
    assert fresh.summary()["online"] == 6
    assert fresh.servers[0]["fault"] is None


def test_real_transport_faults_and_server_disconnect(server):
    from websocket import ABNF
    ws_url = server.replace("http:", "ws:") + "/ws"
    ws = create_connection(ws_url, timeout=3, http_proxy_host=None)
    try:
        assert json.loads(ws.recv())["type"] == "snapshot"
        api(server, "/api/diagnostics/faults", "PUT", {"error_status": 503})
        with pytest.raises(HTTPError) as exc:
            api(server, "/api/health")
        assert exc.value.code == 503
        assert api(server, "/api/diagnostics")["connected_clients"] >= 1
        api(server, "/api/diagnostics/faults", "DELETE")
        assert api(server, "/api/health")["status"] == "ok"
        ws.send(json.dumps({"type": "ping", "id": "live-probe"}))
        pong = receive_until(ws, lambda data: data["type"] == "pong")
        assert pong["id"] == "live-probe"
        api(server, "/api/diagnostics/disconnect", "POST")
        for _ in range(30):
            opcode, data = ws.recv_data(control_frame=True)
            if opcode == ABNF.OPCODE_CLOSE:
                assert int.from_bytes(data[:2], "big") == 1012
                break
        else:
            pytest.fail("Server did not send a close frame")
        replacement = create_connection(ws_url, timeout=3, http_proxy_host=None)
        try:
            assert json.loads(replacement.recv())["type"] == "snapshot"
        finally:
            replacement.close()
    finally:
        ws.close()
        api(server, "/api/diagnostics/faults", "DELETE")
