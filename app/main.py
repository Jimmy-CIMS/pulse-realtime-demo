import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse

from app.diagnostics import FaultSettings, NetworkLab
from app.simulation import Incident, Settings, Simulator, now

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.simulator = Simulator()
    app.state.network = NetworkLab()
    task = asyncio.create_task(app.state.simulator.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="Pulse · Synthetic monitoring API", version="0.1.0",
                  lifespan=lifespan, root_path=os.getenv("ROOT_PATH", ""))

    @app.get("/static/{filename}", include_in_schema=False)
    async def static_file(filename: str):
        # Explicit allowlist also works with proxies that strip their URL prefix.
        if filename not in {"app.js", "diagnostics.js", "style.css"}:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(STATIC / filename)

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        # A trailing slash keeps relative asset/API URLs under code-server's proxy prefix.
        if not request.url.path.endswith("/"):
            return RedirectResponse(str(request.url) + "/")
        return FileResponse(STATIC / "index.html")

    async def read_fault(request: Request):
        # Only read APIs participate; recovery and control endpoints remain available.
        settings = request.app.state.network.current().model_copy()
        if settings.delay_ms:
            await asyncio.sleep(settings.delay_ms / 1000)
        if settings.error_status:
            raise HTTPException(status_code=settings.error_status,
                                detail="Simulated API failure")

    @app.get("/api/diagnostics")
    async def diagnostics(request: Request):
        return request.app.state.network.status()

    @app.put("/api/diagnostics/faults")
    async def configure_faults(settings: FaultSettings, request: Request):
        request.app.state.network.configure(settings)
        request.app.state.simulator.event("warning", "Transport fault settings updated")
        request.app.state.simulator.publish()
        return request.app.state.network.status()

    @app.delete("/api/diagnostics/faults")
    async def clear_faults(request: Request):
        request.app.state.network.reset()
        request.app.state.simulator.event("info", "Transport faults cleared")
        request.app.state.simulator.publish()
        return request.app.state.network.status()

    @app.post("/api/diagnostics/disconnect")
    async def disconnect_clients(request: Request):
        count = request.app.state.network.disconnect()
        request.app.state.simulator.event("warning", "WebSocket disconnect requested")
        return {"disconnected_clients": count}

    @app.get("/api/health")
    async def health(request: Request):
        await read_fault(request)
        return {"status": "ok", "synthetic": True, "storage": "memory"}

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        await read_fault(request)
        return request.app.state.simulator.snapshot(include_history=True)

    @app.get("/api/servers")
    async def servers(request: Request):
        await read_fault(request)
        return request.app.state.simulator.servers

    @app.get("/api/settings", response_model=Settings)
    async def settings(request: Request):
        return request.app.state.simulator.settings

    @app.put("/api/settings", response_model=Settings)
    async def update_settings(settings: Settings, request: Request):
        sim = request.app.state.simulator
        sim.settings = settings
        sim.publish()
        return settings

    @app.post("/api/incidents")
    async def inject(incident: Incident, request: Request):
        sim = request.app.state.simulator
        try:
            sim.inject(incident)
        except KeyError:
            raise HTTPException(status_code=404, detail="Server not found") from None
        return sim.snapshot()

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await socket.accept()
        sim = socket.app.state.simulator
        lab = socket.app.state.network
        disconnect = asyncio.Event()
        lab.connections.add(disconnect)
        send_lock = asyncio.Lock()

        async def send_json(data):
            async with send_lock:
                await asyncio.wait_for(socket.send_json(data), timeout=10)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        sim.subscribers.add(queue)

        async def send_updates():
            while True:
                data = await queue.get()
                if not lab.current().pause_stream:
                    await send_json(data)

        async def receive_messages():
            while True:
                text = await socket.receive_text()
                # Legacy text pings remain accepted without changing telemetry messages.
                if len(text) > 256:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if (isinstance(message, dict) and message.get("type") == "ping"
                        and isinstance(message.get("id"), str) and len(message["id"]) <= 64):
                    await send_json({"type": "pong", "id": message["id"], "time": now()})

        async def close_on_request():
            await disconnect.wait()
            async with send_lock:
                await asyncio.wait_for(
                    socket.close(code=1012, reason="Simulated transport disconnect"), timeout=10)

        tasks: list[asyncio.Task] = []
        try:
            await send_json(sim.snapshot(True))
            tasks = [asyncio.create_task(send_updates()), asyncio.create_task(receive_messages()),
                     asyncio.create_task(close_on_request())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            pass
        finally:
            sim.subscribers.discard(queue)
            lab.connections.discard(disconnect)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app


app = create_app()
