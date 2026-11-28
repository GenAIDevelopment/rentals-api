"""
Contract tests for the Ops MCP server, run in memory against a seeded telemetry.db. No AI needed.

  uv run pytest -q
"""
import json
import re
import time
from pathlib import Path

import pytest
from mcp import Client

from ops_mcp.server import mcp
from rentals_api import telemetry

ROUTES = ["/properties", "/properties/{property_id}", "/bookings"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "DB_PATH", tmp_path / "telemetry.db")
    telemetry.init_db()


@pytest.fixture
def incident_db(empty_db):
    """10 healthy minutes on 1.4.0, then 1.4.1 deployed 2 minutes ago, breaking every 3rd listing view."""
    now = time.time()
    deploy_ts = now - 120
    with telemetry.connect() as conn:
        for s in range(600):                                           # 12 → 2 minutes ago, healthy
            ts = now - 720 + s
            for route in ROUTES:
                conn.execute("INSERT INTO requests VALUES (?, 'GET', ?, 200, ?, '1.4.0')", (ts, route, 40 + s % 50))
        conn.execute("INSERT INTO deploys VALUES (?, '1.4.1', 'ravi.k', 'Show amenities on the listing page')",
                     (deploy_ts,))
        for s in range(120):                                           # last 2 minutes on 1.4.1
            ts = deploy_ts + s + 0.5
            for route in ROUTES:
                broken = route == "/properties/{property_id}" and s % 3 == 0
                conn.execute("INSERT INTO requests VALUES (?, 'GET', ?, ?, ?, '1.4.1')",
                             (ts, route, 500 if broken else 200, 40 + s % 50))
                if broken:
                    conn.execute("INSERT INTO logs VALUES (?, 'ERROR', ?, ?, ?)",
                                 (ts, route, "Unhandled KeyError: 'amenities'", json.dumps({"version": "1.4.1"})))
        for i, waited in enumerate([912, 1340, 1777]):
            conn.execute("INSERT INTO logs VALUES (?, 'WARNING', '/properties', ?, NULL)",
                         (now - 30 + i, f"DB connection pool exhausted: waited {waited} ms for a connection"))


async def call(name: str, **args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


def text_of(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


# ─────────────── what the server offers ───────────────
@pytest.mark.anyio
async def test_offers_eight_tools_and_one_action():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert len(tools) == 8
    actions = [n for n, t in tools.items() if not t.annotations.read_only_hint]
    assert actions == ["rollback_deploy"]
    assert tools["rollback_deploy"].annotations.destructive_hint is True


# ─────────────── read tools find the seeded incident ───────────────
@pytest.mark.anyio
async def test_overview_breaches_slo_and_lists_worst_route_first(incident_db):
    result = (await call("service_overview", minutes=2)).structured_content
    assert result["version"] == "1.4.1"
    assert any("error rate" in b for b in result["slo_breaches"])
    assert result["routes"][0]["route"] == "/properties/{property_id}"
    assert result["data_warning"] is None


@pytest.mark.anyio
async def test_compare_windows_names_the_broken_route(incident_db):
    routes = (await call("compare_windows", recent_minutes=2, baseline_minutes=10)).structured_content["routes"]
    assert routes[0]["route"] == "/properties/{property_id}"
    assert routes[0]["verdict"].startswith("errors up")
    assert all(r["verdict"] == "no significant change" for r in routes[1:])


@pytest.mark.anyio
async def test_error_breakdown_counts_errors_and_groups_similar_warnings(incident_db):
    result = (await call("error_breakdown", minutes=15)).structured_content
    assert result["errors"] == [{"route": "/properties/{property_id}", "status": 500, "count": 40}]
    messages = {m["example"]: m for m in result["top_messages"]}
    assert messages["Unhandled KeyError: 'amenities'"]["count"] == 40
    pool = [m for m in result["top_messages"] if "pool exhausted" in m["example"]]
    assert len(pool) == 1 and pool[0]["count"] == 3


@pytest.mark.anyio
async def test_errors_by_version_blames_the_new_version(incident_db):
    versions = (await call("errors_by_version", minutes=15)).structured_content["versions"]
    assert {v["version"]: v["errors_5xx"] for v in versions} == {"1.4.0": 0, "1.4.1": 40}


# ─────────────── bad inputs ───────────────
@pytest.mark.anyio
async def test_unknown_route_lists_the_real_ones(incident_db):
    result = await call("latency_trend", route="/nope")
    assert result.is_error
    assert all(route in text_of(result) for route in ROUTES)


@pytest.mark.anyio
async def test_minutes_out_of_range_is_refused(incident_db):
    assert (await call("service_overview", minutes=500)).is_error


# ─────────────── rollback guardrails ───────────────
@pytest.mark.anyio
@pytest.mark.parametrize("to_version, reason, expected", [
    ("9.9.9", "errors only on the new version", "never deployed"),
    ("1.4.1", "errors only on the new version", "already running"),
    ("1.4.0", "too short", "at least 15"),
])
async def test_rollback_refuses_bad_requests(incident_db, to_version, reason, expected):
    result = await call("rollback_deploy", to_version=to_version, reason=reason)
    assert result.is_error
    assert expected in text_of(result)


@pytest.mark.anyio
async def test_rollback_succeeds_then_rolling_forward_is_refused(incident_db):
    done = await call("rollback_deploy", to_version="1.4.0", reason="KeyError amenities only on 1.4.1")
    assert not done.is_error
    assert done.structured_content["version"] == "1.4.0"
    assert done.structured_content["author"] == "rentals-ops-mcp"
    assert done.structured_content["note"].startswith("Rollback: ")
    forward = await call("rollback_deploy", to_version="1.4.1", reason="trying to go forward again")
    assert forward.is_error
    assert "newer" in text_of(forward)


# ─────────────── honesty ───────────────
@pytest.mark.anyio
async def test_empty_db_warns_and_reports_no_breaches(empty_db):
    result = (await call("service_overview", minutes=5)).structured_content
    assert result["data_warning"].startswith("No requests recorded at all")
    assert result["slo_breaches"] == []


# ─────────────── design rule ───────────────
def test_server_never_reads_the_faults_table():
    for path in (Path(__file__).parent.parent / "ops_mcp").glob("*.py"):
        code = re.sub(r"#.*faults table.*", "", path.read_text(encoding="utf-8"))
        assert "faults" not in code, f"{path.name} mentions faults"
