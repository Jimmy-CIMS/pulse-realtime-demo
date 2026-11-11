"""Temporary transport faults shared by all clients of this development demo."""
import asyncio
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FaultSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay_ms: int = Field(default=0, ge=0, le=15000)
    error_status: Literal[0, 500, 503] = 0
    pause_stream: bool = False
    duration: int = Field(default=30, ge=1, le=120)


class NetworkLab:
    def __init__(self):
        self.settings = FaultSettings()
        self.expires_at = 0.0
        self.connections: set[asyncio.Event] = set()

    def current(self) -> FaultSettings:
        if self.expires_at and time.monotonic() >= self.expires_at:
            self.reset()
        return self.settings

    def configure(self, settings: FaultSettings):
        self.settings = settings
        self.expires_at = time.monotonic() + settings.duration

    def reset(self):
        self.settings = FaultSettings()
        self.expires_at = 0.0

    def status(self) -> dict:
        settings = self.current()
        return {
            "faults": settings.model_dump(),
            "remaining_seconds": round(max(0.0, self.expires_at - time.monotonic()), 1),
            "connected_clients": len(self.connections),
        }

    def disconnect(self) -> int:
        connections = list(self.connections)
        for signal in connections:
            signal.set()
        return len(connections)
