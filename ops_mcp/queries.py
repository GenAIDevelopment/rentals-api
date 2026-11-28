"""
Data layer for the Ops MCP server: every SQL query lives here, and nothing here knows about MCP.

It reads only what a human on-call engineer would see: requests, logs and deploys.
Swap this module for one that talks to Prometheus, Loki or a deploy API, and the tools still work.
"""
import json
import re
import sqlite3
import time

from rentals_api import telemetry


def _connect() -> sqlite3.Connection:
    telemetry.init_db()                     # make sure the tables exist, even on a brand-new file
    return telemetry.connect()


# ─────────────── time and maths helpers ───────────────
def now() -> float:
    return time.time()


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile: p95 = the time within which 95% of requests finished."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
    return round(ordered[index], 1)


def summarize(rows: list) -> dict:
    """Turn many request rows into the handful of numbers an engineer looks at."""
    durations = [r["duration_ms"] for r in rows]
    errors = sum(1 for r in rows if r["status"] >= 500)
    return {
        "requests": len(rows),
        "errors_5xx": errors,
        "error_rate_pct": round(100 * errors / len(rows), 2) if rows else 0.0,
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
    }


# ─────────────── requests ───────────────
def requests_between(since: float, until: float | None = None, route: str | None = None) -> list:
    sql, args = "SELECT * FROM requests WHERE ts >= ?", [since]
    if until is not None:
        sql += " AND ts < ?"
        args.append(until)
    if route:
        sql += " AND route = ?"
        args.append(route)
    with _connect() as conn:
        return conn.execute(sql + " ORDER BY ts", args).fetchall()


def known_routes() -> list[str]:
    with _connect() as conn:
        return [r["route"] for r in conn.execute("SELECT DISTINCT route FROM requests ORDER BY route")]


def last_request_time() -> float | None:
    with _connect() as conn:
        return conn.execute("SELECT MAX(ts) FROM requests").fetchone()[0]


def status_counts(rows: list) -> list[dict]:
    """5xx responses grouped by (route, status), most frequent first."""
    counts: dict[tuple[str, int], int] = {}
    for r in rows:
        if r["status"] >= 500:
            counts[(r["route"], r["status"])] = counts.get((r["route"], r["status"]), 0) + 1
    return [{"route": route, "status": status, "count": n}
            for (route, status), n in sorted(counts.items(), key=lambda kv: -kv[1])]


def errors_by_version(since: float, route: str | None = None) -> list[dict]:
    """Per version: requests, 5xx errors, error rate and p95, in the window."""
    by_version: dict[str, list] = {}
    for r in requests_between(since, route=route):
        by_version.setdefault(r["version"], []).append(r)
    return [{"version": v, **summarize(rows)} for v, rows in sorted(by_version.items())]


# ─────────────── logs ───────────────
def logs_between(since: float, levels: list[str] | None = None, contains: str | None = None,
                 route: str | None = None, limit: int = 20, newest_first: bool = True) -> list:
    sql, args = "SELECT * FROM logs WHERE ts >= ?", [since]
    if levels:
        sql += f" AND level IN ({', '.join('?' for _ in levels)})"
        args.extend(levels)
    if contains:
        sql += " AND message LIKE ?"
        args.append(f"%{contains}%")
    if route:
        sql += " AND route = ?"
        args.append(route)
    sql += f" ORDER BY ts {'DESC' if newest_first else 'ASC'} LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        return conn.execute(sql, args).fetchall()


def parse_extra(extra: str | None) -> dict:
    return json.loads(extra) if extra else {}


def message_pattern(msg: str) -> str:
    """'waited 1234 ms' and 'waited 987 ms' are the same problem: replace numbers with #."""
    return re.sub(r"\d+(\.\d+)?", "#", msg)


def group_messages(lines: list) -> list[dict]:
    """Group log lines by (level, pattern), most frequent first."""
    groups: dict[tuple[str, str], dict] = {}
    for line in lines:
        key = (line["level"], message_pattern(line["message"]))
        g = groups.setdefault(key, {"level": line["level"], "example": line["message"], "count": 0,
                                    "first_seen": line["ts"], "last_seen": line["ts"]})
        g["count"] += 1
        g["first_seen"] = min(g["first_seen"], line["ts"])
        g["last_seen"] = max(g["last_seen"], line["ts"])
    return sorted(groups.values(), key=lambda g: -g["count"])


# ─────────────── deploys ───────────────
def deploys(limit: int = 5) -> list:
    with _connect() as conn:
        return conn.execute("SELECT * FROM deploys ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()


def known_versions() -> list[str]:
    with _connect() as conn:
        return [r["version"] for r in conn.execute(
            "SELECT version FROM deploys GROUP BY version ORDER BY MIN(ts)")]


def current_version() -> str | None:
    latest = deploys(1)
    return latest[0]["version"] if latest else None


def record_rollback(version: str, author: str, reason: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO deploys VALUES (?, ?, ?, ?)", (now(), version, author, f"Rollback: {reason}"))
