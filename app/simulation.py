"""One process-wide simulator; all values are synthetic and held in memory."""
import asyncio
import random
from collections import deque
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool = False
    interval: float = Field(default=1, ge=0.25, le=5)


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: str
    kind: Literal["cpu", "offline", "recover"]


class Simulator:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.settings = Settings()
        self.sequence = 0
        self.history: deque[dict] = deque(maxlen=1201)
        self.events: deque[dict] = deque(maxlen=60)
        self.subscribers: set[asyncio.Queue] = set()
        self.servers: list[dict] = [
            {"id": f"srv-{i + 1:02}", "name": name, "region": region,
             "cpu": self.rng.uniform(15, 55), "memory": self.rng.uniform(30, 65),
             "network": self.rng.uniform(20, 90), "fault": None, "status": "healthy"}
            for i, (name, region) in enumerate([
                ("gateway-01", "Taipei"), ("api-01", "Taipei"), ("api-02", "Tokyo"),
                ("worker-01", "Tokyo"), ("cache-01", "Singapore"), ("worker-02", "Singapore")
            ])
        ]
        self.event("info", "Simulation started. All data is randomly generated.")
        self.tick()

    def event(self, level: str, message: str):
        self.events.appendleft({"time": now(), "level": level, "message": message})

    def tick(self):
        for server in self.servers:
            old_status = server["status"]
            for key, drift, low, high in [
                ("cpu", 5, 4, 86), ("memory", 2, 15, 90), ("network", 9, 1, 150)
            ]:
                server[key] = round(max(low, min(high, server[key] + self.rng.uniform(-drift, drift))), 1)
            if server["fault"] == "cpu":
                server["cpu"] = round(self.rng.uniform(92, 99), 1)
            if server["fault"] == "offline":
                server.update(cpu=0, memory=0, network=0, status="offline")
            else:
                server["status"] = "warning" if server["cpu"] >= 80 else "healthy"
            if old_status != server["status"]:
                self.event("info" if server["status"] == "healthy" else "warning",
                           f'{server["name"]} status: {server["status"]}')
        self.sequence += 1
        point = {"time": now(), **self.summary()}
        self.history.append(point)
        cutoff = datetime.now(timezone.utc).timestamp() - 300
        while self.history and datetime.fromisoformat(self.history[0]["time"]).timestamp() < cutoff:
            self.history.popleft()

    def summary(self) -> dict:
        active = [s for s in self.servers if s["status"] != "offline"]
        return {
            "cpu": round(sum(s["cpu"] for s in active) / max(len(active), 1), 1),
            "memory": round(sum(s["memory"] for s in active) / max(len(active), 1), 1),
            "network": round(sum(s["network"] for s in active), 1),
            "online": len(active),
            "warnings": sum(s["status"] != "healthy" for s in self.servers),
        }

    def snapshot(self, include_history: bool = False) -> dict:
        data = {"type": "snapshot" if include_history else "update", "time": now(),
                "sequence": self.sequence, "synthetic": True,
                "settings": self.settings.model_dump(), "summary": self.summary(),
                "servers": [dict(s) for s in self.servers], "events": list(self.events),
                "point": self.history[-1] if self.history else None}
        if include_history:
            data["history"] = list(self.history)
        return data

    def publish(self):
        data = self.snapshot()
        for queue in self.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(data)

    def inject(self, incident: Incident):
        server = next((s for s in self.servers if s["id"] == incident.server_id), None)
        if server is None:
            raise KeyError(incident.server_id)
        server["fault"] = None if incident.kind == "recover" else incident.kind
        if incident.kind == "recover":
            server.update(cpu=35.0, memory=48.0, network=50.0, status="healthy")
        self.event("info" if incident.kind == "recover" else "warning",
                   f'{server["name"]}: ' + {"cpu": "CPU spike injected", "offline": "Service taken offline",
                                          "recover": "Service restored"}[incident.kind])
        # Explicit actions are applied even when the automatic simulation is paused.
        self.tick()
        self.publish()

    async def run(self):
        while True:
            await asyncio.sleep(self.settings.interval)
            if not self.settings.paused:
                self.tick()
            # Also sends a heartbeat while paused.
            self.publish()
