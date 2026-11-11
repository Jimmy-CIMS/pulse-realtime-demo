# Pulse Realtime Demo

A Python FastAPI monitoring dashboard with WebSocket updates, simulated server metrics, and fault injection for testing code-server development workflows.

All CPU, memory, and network metrics are randomly generated. The application uses in-memory state, requires no database, and never reads real host metrics.

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uv sync --locked
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/`. In code-server, open port **8000** from the Ports panel, or visit `/proxy/8000/` under your code-server URL. Keep the trailing slash so relative URLs resolve correctly.

The dashboard uses plain HTML, CSS, JavaScript, and Canvas. No Node.js installation, frontend build, or CDN is required for the dashboard.

### Without uv

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1' 'pytest>=8,<10' 'websocket-client>=1.8,<2'
.venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

These commands assume a Unix-like shell. On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`. The uv workflow installs the exact versions recorded in `uv.lock`; the pip alternative installs versions within the declared ranges.

## Features

- Six simulated servers across three regions, with live metrics and status filtering.
- CPU and memory trends covering up to five minutes of collected data.
- Update intervals of 0.25, 0.5, 1, 2, or 5 seconds.
- Pause and resume, with heartbeats continuing while paused.
- CPU spikes, service outages, and manual recovery for a selected server.
- Shared state across browser tabs and WebSocket clients.
- Automatic reconnection with backoff and a fresh snapshot after reconnecting.
- Connection status, message count, last sync time, and an event log.

Offline servers are excluded from average CPU and memory usage. Network traffic is summed across online servers. CPU usage of 80% or higher triggers a warning. Injected faults persist until recovery; restarting the application resets all state. History is limited to five minutes and 1,201 samples, with the latest 60 events retained.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Health check |
| GET | `/api/snapshot` | Servers, summary, settings, events, and history |
| GET | `/api/servers` | Simulated server list |
| GET / PUT | `/api/settings` | Read or replace simulation settings |
| POST | `/api/incidents` | Inject a fault or restore a server |
| WebSocket | `/ws` | Initial snapshot followed by live updates |

Interactive documentation is available at `docs`, with the schema at `openapi.json`, relative to the application URL. Swagger UI loads its assets from a CDN and requires browser internet access.

Replace simulation settings with a complete object:

```json
{"paused": false, "interval": 1}
```

Inject a fault:

```json
{"server_id": "srv-01", "kind": "cpu"}
```

Incident kinds are `cpu`, `offline`, and `recover`. Intervals must be between 0.25 and 5 seconds. Invalid input returns HTTP 422; an unknown server returns HTTP 404.

The first WebSocket message has `type: "snapshot"` and includes `history`. Subsequent messages have `type: "update"` and include the latest `point`. Both contain `sequence`, UTC `time`, `synthetic`, `settings`, `servers`, `summary`, and `events`. Sequence numbers increase when simulation data changes; paused heartbeats can repeat a sequence number. Slow clients retain only the latest pending update and can reconnect to retrieve history.

## code-server and reverse proxies

Assets, API requests, and WebSocket URLs resolve relative to the visible application directory, supporting subdomains and paths such as `/proxy/8000/`. HTTPS pages automatically use `wss://`. Any intermediate reverse proxy must support WebSocket Upgrade.

If Swagger UI cannot find its schema behind a path-based proxy, set the external prefix:

```sh
ROOT_PATH=/proxy/8000 uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Use the complete external prefix without a trailing slash, including any prefix used by code-server itself.

Run a **single Uvicorn worker**: each process has its own in-memory simulator. The demo has no application login and is intended for a development preview protected by code-server access controls.

## Development and testing

```sh
uv sync --locked
uv run pytest -q
uv run mypy app
```

GitHub Actions runs tests and type checks on Python 3.10 and 3.12 for pushes and pull requests. The workflow uses locked dependencies through [uv's GitHub Actions integration](https://docs.astral.sh/uv/guides/integration/github/).

Tests cover HTTP routes, assets, proxy prefixes, validation, pause/resume, fault recovery, shared WebSocket updates, reconnect snapshots, disconnect cleanup, bounded history and events, and slow-client queues.

- `tests/test_asgi.py` exercises HTTP and WebSocket routes in-process without opening ports.
- `tests/test_app.py` starts a temporary Uvicorn server for actual HTTP and WebSocket connections. Network tests are explicitly skipped when the environment prohibits socket creation.

For manual verification, open two tabs, inject and recover an outage, pause and confirm metrics stop changing, then restart the backend and observe reconnection.

### Debugging in code-server

Install the recommended Python, Python Debugger, and Ruff extensions, and select `.venv/bin/python` as the interpreter. Choose **Run and Debug → Pulse: FastAPI debugger** to use breakpoints in API handlers or `Simulator.tick()`.

The debugger runs without reload to keep breakpoints predictable. Use **Tasks → Pulse: dev** for normal development with automatic reload. Shared editor settings, launch configurations, and tasks in `.vscode/` are intended to be committed.

## Repository files

```text
app/main.py           API, WebSocket handling, and application lifecycle
app/simulation.py     Shared in-memory simulator
app/static/           Dashboard HTML, CSS, and JavaScript
tests/                ASGI and real-network tests
.vscode/              Shared editor, task, and debug configuration
.github/workflows/    GitHub Actions CI
pyproject.toml        Dependencies and tool settings
uv.lock               Locked dependency versions; commit this file
.python-version       Default development Python version
```

`.gitignore` excludes virtual environments, caches, local environment files, credentials, and local agent state. `.gitattributes` and `.editorconfig` keep text files consistent across platforms.

## Push to GitHub

Create an empty GitHub repository named `pulse-realtime-demo`, then run these commands from the project directory, replacing `YOUR_USERNAME`:

```sh
git init -b main
git add .
git commit -m "feat: add realtime monitoring demo"
git remote add origin https://github.com/YOUR_USERNAME/pulse-realtime-demo.git
git push -u origin main
```

If the directory is already a Git repository, skip `git init`. If `origin` already exists, use `git remote set-url origin ...` instead of adding it again.
