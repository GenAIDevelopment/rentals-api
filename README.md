# Rentals API

**StayEase Rentals API**: a FastAPI service for searching and booking rental homes, with built-in
telemetry, plus simulation services that put it under load and break it on purpose.

```text
 simulation/loadgen.py ──HTTP traffic──▶ rentals_api (FastAPI) ──records──▶ telemetry.db ──▶ scripts/peek.py
 simulation/chaos.py ───flips faults───▶        │                            requests · logs · deploys
```

## Project layout

```text
rentals-api/
├── rentals_api/            ← Part 1: the application
│   ├── main.py             the FastAPI app: properties and bookings endpoints
│   ├── telemetry.py        records every request, log line and deploy into telemetry.db
│   └── faults.py           fault-injection hooks (all off by default)
├── simulation/             ← Part 2: simulation services
│   ├── loadgen.py          synthetic users that search, view and book non-stop
│   └── chaos.py            switches faults on and off, ships a buggy release
├── scripts/
│   └── peek.py             a terminal dashboard over telemetry.db
├── ops_mcp/                ← Part 3: the Ops MCP server (built in class)
│   ├── queries.py          all the SQL: requests, logs and deploys (never the faults table)
│   ├── server.py           8 tools, 3 resources, 2 prompts
│   └── call.py             try the tools from the terminal, no AI needed
├── tests/
│   └── test_ops_mcp.py     contract tests for the MCP server
├── pyproject.toml          dependencies
├── uv.lock                 exact versions, the same for everyone
└── .python-version         Python 3.12
```

---

## Setup

### 1 · Install uv (one time)

**Windows** (PowerShell):
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a **new** terminal and check with `uv --version`.

### 2 · Get the code and install

```powershell
cd D:\Training\GenAI\Projects               # or any folder you like
git clone <repository URL shared by your trainer>
cd rentals-api
uv sync
```

`uv sync` downloads Python 3.12 if needed, creates a private `.venv` folder, and installs the exact versions in `uv.lock`.

> Run every command below from the `rentals-api` folder.

---

## Part 1 · The Rentals API

### Run it

```powershell
uv run uvicorn rentals_api.main:app --port 9000
```

Open **http://127.0.0.1:9000/docs** to try every endpoint in the browser.

| Method | Endpoint | Does |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/properties?city=Hyderabad&max_rent=40000` | Search listings |
| GET | `/properties/{property_id}` | One listing (IDs 101–108) |
| POST | `/bookings` | Book: `{"property_id": 102, "tenant": "Asha", "months": 11}` |
| GET | `/bookings/{booking_id}` | One booking |

### How the telemetry works

A FastAPI **middleware** in `main.py` wraps every request. It times the request and writes one row to `telemetry.db`: method, **route template** (such as `/properties/{property_id}`), status code, duration and app version. The API also writes **structured log lines** (level, route, message and details), and `telemetry.db` keeps a history of **deploys**.

After a few requests, look at it:

```powershell
uv run python -m scripts.peek
```

---

## Part 2 · Simulation services

Use **three terminal windows**, all in the `rentals-api` folder:

| Window | Command | You should see |
|---|---|---|
| 1 · API | `uv run uvicorn rentals_api.main:app --port 9000` | `Uvicorn running on http://127.0.0.1:9000` |
| 2 · load | `uv run python -m simulation.loadgen` | About 19 req/s, reported every 10 seconds |
| 3 · you | `uv run python -m scripts.peek` | Requests, errors and latency per route |

### Break it on purpose

```powershell
uv run python -m simulation.chaos status
uv run python -m simulation.chaos bad_deploy             # ship buggy version 1.4.1
#   ...wait a minute...
uv run python -m scripts.peek 1
uv run python -m simulation.chaos reset
```

| Incident | Command | What to look for in `peek` |
|---|---|---|
| Bad deploy | `simulation.chaos bad_deploy` | Errors on one route, right after a new version |
| Slow database | `simulation.chaos slow_db on` | Every route slow; "connection pool exhausted" warnings |
| Payment timeout | `simulation.chaos payment_timeout on` | Only bookings failing, with 502 errors |

Turn faults off with `off`, or everything at once with `reset`.

---

## Part 3 · The Ops MCP server

`ops_mcp` lets an AI assistant investigate incidents like an on-call engineer. It sees only what a human would: requests, logs and deploys. It has seven read-only tools and **one** action, `rollback_deploy`, which is guarded and should only be used after you say yes.

| Tool | Answers |
|---|---|
| `service_overview` | Is it broken right now? Traffic, errors, p95, SLO breaches, worst routes |
| `compare_windows` | What changed compared with the baseline? |
| `latency_trend` | When did it start, minute by minute, for one route? |
| `error_breakdown` | What is failing? 5xx by route, plus grouped error and warning messages |
| `search_logs` | What exactly did the logs say? |
| `recent_deploys` | What shipped recently? |
| `errors_by_version` | Is the new version to blame? |
| `rollback_deploy` | **Action.** Go back to an earlier version |

It also has resources (`ops://slo`, `ops://runbook`, `ops://status`) and prompts (`investigate`, `incident_report`).

### Try it without an AI

Keep the API and the load generator running (Part 2), then:

```powershell
uv run python -m ops_mcp.call --list
uv run python -m ops_mcp.call service_overview minutes=5
uv run python -m ops_mcp.call compare_windows recent_minutes=2 baseline_minutes=10
uv run python -m ops_mcp.call --resource ops://runbook
uv run pytest -q                                   # 13 passed
```

Or open the **MCP Inspector** in the browser (needs Node.js; the first start takes about 30 s):

```powershell
uv run mcp dev ops_mcp/server.py
```

Open the page it prints (http://localhost:6274) and click **Connect**. Everything is already filled in.

### Connect it to Claude Desktop (stdio)

Claude Desktop starts the server itself and talks to it over **stdio** (standard input and output). You don't run the server in a terminal yourself.

**1 · Find two paths** (in PowerShell, from the `rentals-api` folder):

```powershell
(Get-Command uv).Source      # e.g. C:\Users\<you>\.local\bin\uv.exe
(Get-Location).Path          # e.g. D:\Training\GenAI\Projects\rentals-api
```

**2 · Open the config file:** in Claude Desktop, go to **Settings → Developer → Edit Config**. That opens `claude_desktop_config.json` (usually `%APPDATA%\Claude\claude_desktop_config.json`).

**3 · Add the server.** Put in your own two paths, and use **double backslashes** (`\\`) in JSON:

```json
{
  "mcpServers": {
    "rentals-ops": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": ["run", "--directory", "D:\\Training\\GenAI\\Projects\\rentals-api",
               "python", "-m", "ops_mcp.server"]
    }
  }
}
```

If you'd rather not use uv here, point at the project's own Python. In that case `PYTHONPATH` is **required**:

```json
{
  "mcpServers": {
    "rentals-ops": {
      "command": "D:\\Training\\GenAI\\Projects\\rentals-api\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ops_mcp.server"],
      "env": { "PYTHONPATH": "D:\\Training\\GenAI\\Projects\\rentals-api" }
    }
  }
}
```

> Claude starts the server from another folder, so a config with just `.venv\Scripts\python.exe -m ops_mcp.server` fails with `No module named 'ops_mcp'`. Use `--directory` or `PYTHONPATH` as shown.

If the file already has other settings, add `"rentals-ops"` inside the existing `"mcpServers"` block instead of pasting a second one.

**4 · Restart Claude Desktop completely:** right-click its icon in the **system tray** and choose **Quit**. Closing the window is not enough. Then open it again.

**5 · Check it's connected:** in a new chat, open the **tools menu** (the slider icon under the message box). It should list **rentals-ops** with 8 tools. The prompts *Investigate an incident* and *Write an incident report* are in the **+** menu.

If it doesn't appear, read `%APPDATA%\Claude\logs\mcp-server-rentals-ops.log`.

### Sample questions

Start the API and the load generator, and wait a few minutes so there is a baseline. Then break something with `simulation.chaos` and ask Claude:

**Health checks (any time)**
- "Is the rentals service healthy right now?"
- "What's our p95 latency and error rate over the last 15 minutes? Are we within SLO?"
- "What was deployed in the last day, and by whom?"

**After `simulation.chaos bad_deploy`**
- "Users say some listing pages are broken. Investigate and tell me the root cause."
- "Did the latest deploy cause the errors? Show me the evidence by version."
- "When exactly did the errors on `/properties/{property_id}` start?"
- "Roll back if you think it's needed." *(Claude should show the evidence and ask you first.)*

**After `simulation.chaos slow_db on`**
- "Everything feels slow. What's going on?"
- "Should we roll back?" *(It should say no: errors are flat on every version and the runbook says scale the database.)*
- "Search the logs for connection pool warnings in the last 5 minutes."

**After `simulation.chaos payment_timeout on`**
- "Customers can't complete bookings. Which route is failing, and why?"
- "Show me the most common error messages in the last 10 minutes."

**Wrap-up**
- Use the *Write an incident report* prompt, or ask: "Write a short incident report for the last 30 minutes. Use only numbers from the tools."
- Stop the load generator for a minute and ask: "Is the service healthy?" *(It should warn that no recent data is coming in.)*

Run `simulation.chaos reset` between incidents.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `'uv' is not recognized` | Open a new terminal after installing uv |
| `uv sync` fails behind a college or office proxy | Set `HTTPS_PROXY`, or try from another network |
| `No module named 'rentals_api'` | Run commands from the `rentals-api` folder, and use `-m`: `python -m simulation.chaos`, not `python simulation/chaos.py` |
| `Address already in use` on port 9000 | Another copy is running. Close it, or use `--port 9001` (and `simulation.loadgen --url http://127.0.0.1:9001`) |
| `peek` says "No requests yet" | The API (window 1) and the load generator (window 2) must be running |
| Clean start | Stop everything and delete `telemetry.db`, `telemetry.db-shm`, `telemetry.db-wal` |
| Command Prompt won't switch to D: | Use `cd /d D:\...` |
| Claude Desktop: `No module named 'ops_mcp'` | Use `--directory` (uv) or `PYTHONPATH` (python.exe) in the config, as in Part 3 |
| Claude Desktop doesn't show `rentals-ops` | Check the JSON (double backslashes, no trailing commas), then **Quit** from the system tray and reopen. Logs: `%APPDATA%\Claude\logs\mcp-server-rentals-ops.log` |
| Tools say "No requests recorded" | The API and the load generator must be running; the tools only read `telemetry.db` |
| `curl` behaves oddly in PowerShell | Use `curl.exe`, not `curl` |
