import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse

from app.simulation import Incident, Settings, Simulator

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.simulator = Simulator()
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
        if filename not in {"app.js", "style.css"}:
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(STATIC / filename)

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        # A trailing slash keeps relative asset/API URLs under code-server's proxy prefix.
        if not request.url.path.endswith("/"):
            return RedirectResponse(str(request.url) + "/")
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "synthetic": True, "storage": "memory"}

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        return request.app.state.simulator.snapshot(include_history=True)

    @app.get("/api/servers")
    async def servers(request: Request):
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
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        sim.subscribers.add(queue)

        async def send_updates():
            while True:
                await asyncio.wait_for(socket.send_json(await queue.get()), timeout=10)

        async def receive_messages():
            while True:
                await socket.receive_text()

        tasks: list[asyncio.Task] = []
        try:
            await asyncio.wait_for(socket.send_json(sim.snapshot(True)), timeout=10)
            tasks = [asyncio.create_task(send_updates()), asyncio.create_task(receive_messages())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            pass
        finally:
            sim.subscribers.discard(queue)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app


app = create_app()
