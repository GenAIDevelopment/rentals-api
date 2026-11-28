"""
Rentals Ops: an MCP server that lets an assistant investigate StayEase Rentals API incidents
the way an on-call engineer would: requests, logs and deploys. One guarded action: rollback.

  uv run python -m ops_mcp.server            stdio (for a desktop host)
  uv run python -m ops_mcp.server --http     streamable HTTP on http://127.0.0.1:8000/mcp

Never print() here: stdout is the stdio protocol channel. Log to stderr.
"""
import logging
import sys
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ops_mcp import queries as q

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rentals-ops")

SLO = {"p95_ms": 300, "error_rate_pct": 1.0}
STALE_AFTER_S = 60
ROLLBACK_AUTHOR = "rentals-ops-mcp"

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
Minutes = Annotated[int, Field(ge=1, le=120, description="How many minutes back to look (1-120).")]
Route = Annotated[str | None, Field(description="Route template, e.g. /properties/{property_id}. Omit for all routes.")]

mcp = MCPServer(
    "Rentals Ops",
    instructions=(
        "On-call assistant for the StayEase Rentals API. Investigate in this order: "
        "service_overview → compare_windows → latency_trend → error_breakdown / search_logs → "
        "recent_deploys + errors_by_version → match the ops://runbook. "
        "Quote every number with its time window and as_of time, and mention any data_warning. "
        "Always ask the person before calling rollback_deploy."
    ),
)


# ─────────────── result models ───────────────
class RouteStatus(BaseModel):
    route: str
    requests: int
    error_rate_pct: float
    p95_ms: float


class Overview(BaseModel):
    as_of: str
    window_minutes: int
    version: str
    requests_per_min: float
    error_rate_pct: float
    p50_ms: float
    p95_ms: float
    slo_breaches: list[str]
    data_warning: str | None
    routes: list[RouteStatus]


class Deploy(BaseModel):
    time: str
    minutes_ago: float
    version: str
    author: str
    note: str


class StatusCount(BaseModel):
    route: str
    status: int
    count: int


class MessageGroup(BaseModel):
    level: str
    example: str
    count: int
    first_seen: str
    last_seen: str


class ErrorBreakdown(BaseModel):
    as_of: str
    window_minutes: int
    total_5xx: int
    errors: list[StatusCount]
    top_messages: list[MessageGroup]


class LogLine(BaseModel):
    time: str
    level: str
    route: str | None
    message: str
    details: dict


class LogSearch(BaseModel):
    as_of: str
    window_minutes: int
    lines: list[LogLine]


class RouteComparison(BaseModel):
    route: str
    baseline_requests: int
    recent_requests: int
    baseline_p95_ms: float
    recent_p95_ms: float
    baseline_error_pct: float
    recent_error_pct: float
    verdict: str


class Comparison(BaseModel):
    as_of: str
    recent_minutes: int
    baseline_minutes: int
    routes: list[RouteComparison]


class MinuteBucket(BaseModel):
    minute: str
    requests: int
    p95_ms: float
    error_rate_pct: float


class Trend(BaseModel):
    as_of: str
    route: str
    window_minutes: int
    minutes: list[MinuteBucket]


class VersionStats(BaseModel):
    version: str
    requests: int
    errors_5xx: int
    error_rate_pct: float
    p95_ms: float


class VersionSplit(BaseModel):
    as_of: str
    window_minutes: int
    route: str | None
    versions: list[VersionStats]


# ─────────────── helpers ───────────────
def check_route(route: str | None) -> None:
    if route and route not in (known := q.known_routes()):
        raise ToolError(f"Unknown route {route!r}. Routes seen in the data: {', '.join(known) or 'none yet'}. "
                        "Use one of these exactly, or omit route.")


def deploy_model(row, now: float) -> Deploy:
    return Deploy(time=q.clock(row["ts"]), minutes_ago=round((now - row["ts"]) / 60, 1),
                  version=row["version"], author=row["author"], note=row["note"])


def version_tuple(v: str) -> tuple[int, ...]:
    """'1.4.10' → (1, 4, 10), so 1.4.10 sorts after 1.4.9."""
    try:
        return tuple(int(part) for part in v.split("."))
    except ValueError:
        raise ToolError(f"Version {v!r} is not in the form MAJOR.MINOR.PATCH.") from None


# ─────────────── read-only tools ───────────────
@mcp.tool(annotations=READ_ONLY)
def service_overview(minutes: Minutes = 5) -> Overview:
    """Is the service healthy right now? Traffic, error rate, latency, SLO breaches, and routes
    worst first. Start every investigation here."""
    now = q.now()
    rows = q.requests_between(now - minutes * 60)
    total = q.summarize(rows)

    routes = [RouteStatus(route=route, **{k: s[k] for k in ("requests", "error_rate_pct", "p95_ms")})
              for route in {r["route"] for r in rows}
              for s in [q.summarize([r for r in rows if r["route"] == route])]]
    routes.sort(key=lambda r: (r.error_rate_pct, r.p95_ms), reverse=True)

    breaches = []
    if rows and total["p95_ms"] > SLO["p95_ms"]:
        breaches.append(f"p95 latency {total['p95_ms']} ms > SLO {SLO['p95_ms']} ms")
    if rows and total["error_rate_pct"] > SLO["error_rate_pct"]:
        breaches.append(f"error rate {total['error_rate_pct']}% > SLO {SLO['error_rate_pct']}%")

    warning = None
    last = q.last_request_time()
    if last is None:
        warning = "No requests recorded at all. Is the API running and receiving traffic?"
    elif now - last > STALE_AFTER_S:
        warning = (f"No requests for {now - last:.0f} s (last at {q.clock(last)}). "
                   "The API may be down, or there is no traffic; these numbers may be stale.")

    return Overview(as_of=q.clock(now), window_minutes=minutes, version=q.current_version() or "unknown",
                    requests_per_min=round(total["requests"] / minutes, 1),
                    error_rate_pct=total["error_rate_pct"], p50_ms=total["p50_ms"], p95_ms=total["p95_ms"],
                    slo_breaches=breaches, data_warning=warning, routes=routes)


@mcp.tool(annotations=READ_ONLY)
def recent_deploys(limit: Annotated[int, Field(ge=1, le=20, description="How many deploys (1-20).")] = 5
                   ) -> list[Deploy]:
    """What was deployed recently, and when? Newest first. A deploy near the start of an incident is a
    suspect, not proof: check errors_by_version before blaming it."""
    now = q.now()
    return [deploy_model(row, now) for row in q.deploys(limit)]


@mcp.tool(annotations=READ_ONLY)
def error_breakdown(minutes: Minutes = 15) -> ErrorBreakdown:
    """What is failing? 5xx responses by route and status, plus the most frequent ERROR and WARNING
    messages (similar messages grouped, with first and last seen)."""
    now = q.now()
    since = now - minutes * 60
    errors = q.status_counts(q.requests_between(since))
    groups = q.group_messages(q.logs_between(since, levels=["ERROR", "WARNING"], limit=100_000))[:5]
    return ErrorBreakdown(
        as_of=q.clock(now), window_minutes=minutes, total_5xx=sum(e["count"] for e in errors),
        errors=[StatusCount(**e) for e in errors],
        top_messages=[MessageGroup(level=g["level"], example=g["example"], count=g["count"],
                                   first_seen=q.clock(g["first_seen"]), last_seen=q.clock(g["last_seen"]))
                      for g in groups])


@mcp.tool(annotations=READ_ONLY)
def search_logs(minutes: Minutes = 15,
                level: Annotated[Literal["INFO", "WARNING", "ERROR"] | None,
                                 Field(description="Only this level. Omit for all.")] = None,
                contains: Annotated[str | None, Field(max_length=200, description="Text the message must contain.")] = None,
                route: Route = None,
                limit: Annotated[int, Field(ge=1, le=50, description="Max lines (1-50).")] = 20) -> LogSearch:
    """What exactly did the logs say? Matching log lines, newest first, with their details."""
    check_route(route)
    now = q.now()
    rows = q.logs_between(now - minutes * 60, levels=[level] if level else None,
                          contains=contains, route=route, limit=limit)
    return LogSearch(as_of=q.clock(now), window_minutes=minutes, lines=[
        LogLine(time=q.clock(r["ts"]), level=r["level"], route=r["route"], message=r["message"],
                details=q.parse_extra(r["extra"])) for r in rows])


@mcp.tool(annotations=READ_ONLY)
def compare_windows(recent_minutes: Annotated[int, Field(ge=1, le=60, description="Recent window (1-60 min).")] = 5,
                    baseline_minutes: Annotated[int, Field(ge=1, le=120, description="Baseline just before it (1-120 min).")] = 30
                    ) -> Comparison:
    """What changed? Each route's recent p95 and error rate against the baseline window just before it,
    with a verdict. Problems first."""
    now = q.now()
    split = now - recent_minutes * 60
    recent = q.requests_between(split)
    baseline = q.requests_between(split - baseline_minutes * 60, until=split)

    out = []
    for route in sorted({r["route"] for r in recent} | {r["route"] for r in baseline}):
        b = q.summarize([r for r in baseline if r["route"] == route])
        n = q.summarize([r for r in recent if r["route"] == route])
        if b["requests"] < 10 and n["requests"] < 10:
            continue
        if n["p95_ms"] > 2 * b["p95_ms"] and n["p95_ms"] > SLO["p95_ms"] and b["p95_ms"] > 0:
            verdict = f"latency {n['p95_ms'] / b['p95_ms']:.1f}x worse"
        elif n["error_rate_pct"] - b["error_rate_pct"] > 2:
            verdict = f"errors up {b['error_rate_pct']}% → {n['error_rate_pct']}%"
        else:
            verdict = "no significant change"
        out.append(RouteComparison(route=route, baseline_requests=b["requests"], recent_requests=n["requests"],
                                   baseline_p95_ms=b["p95_ms"], recent_p95_ms=n["p95_ms"],
                                   baseline_error_pct=b["error_rate_pct"], recent_error_pct=n["error_rate_pct"],
                                   verdict=verdict))
    out.sort(key=lambda c: c.verdict == "no significant change")
    return Comparison(as_of=q.clock(now), recent_minutes=recent_minutes, baseline_minutes=baseline_minutes, routes=out)


@mcp.tool(annotations=READ_ONLY)
def latency_trend(route: Annotated[str, Field(description="Route template, e.g. /properties/{property_id}.")],
                  minutes: Annotated[int, Field(ge=1, le=60, description="How many minutes back (1-60).")] = 15
                  ) -> Trend:
    """When did it start, and is it getting better? One route, minute by minute: requests, p95, error %."""
    check_route(route)
    now = q.now()
    buckets: dict[int, list] = {}
    for r in q.requests_between(now - minutes * 60, route=route):
        buckets.setdefault(int(r["ts"] // 60), []).append(r)
    return Trend(as_of=q.clock(now), route=route, window_minutes=minutes, minutes=[
        MinuteBucket(minute=q.clock(m * 60)[:5], requests=s["requests"], p95_ms=s["p95_ms"],
                     error_rate_pct=s["error_rate_pct"])
        for m, rows in sorted(buckets.items()) for s in [q.summarize(rows)]])


@mcp.tool(annotations=READ_ONLY)
def errors_by_version(minutes: Minutes = 15, route: Route = None) -> VersionSplit:
    """Is the new version to blame? Requests, 5xx errors, error rate and p95 per app version.
    Errors only on the newest version point at the deploy; errors on every version point elsewhere."""
    check_route(route)
    now = q.now()
    return VersionSplit(as_of=q.clock(now), window_minutes=minutes, route=route, versions=[
        VersionStats(version=v["version"], requests=v["requests"], errors_5xx=v["errors_5xx"],
                     error_rate_pct=v["error_rate_pct"], p95_ms=v["p95_ms"])
        for v in q.errors_by_version(now - minutes * 60, route)])


# ─────────────── the one action ───────────────
@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                      idempotent_hint=True, open_world_hint=False))
def rollback_deploy(to_version: Annotated[str, Field(description="An earlier version to go back to, e.g. 1.4.0.")],
                    reason: Annotated[str, Field(min_length=15, max_length=500,
                                                 description="Why, with the evidence (min 15 characters).")]
                    ) -> Deploy:
    """Should we roll back? Records a rollback to an earlier, already-deployed version.
    Use ONLY when errors started with a deploy and appear only on the new version.
    Never for a slow database or payment-provider problems: rolling back will not fix those.
    Ask the person and get a yes before calling this."""
    versions = q.known_versions()
    if to_version not in versions:
        raise ToolError(f"Version {to_version!r} was never deployed. Known versions: {', '.join(versions)}.")
    current = q.current_version()
    if to_version == current:
        raise ToolError(f"{to_version} is already running. Nothing to roll back.")
    if current and version_tuple(to_version) > version_tuple(current):
        raise ToolError(f"{to_version} is newer than the running {current}. A rollback only goes back; "
                        "rolling forward needs a normal deploy.")
    q.record_rollback(to_version, ROLLBACK_AUTHOR, reason)
    log.warning("ROLLBACK to %s: %s", to_version, reason)
    return deploy_model(q.deploys(1)[0], q.now())


# ─────────────── resources ───────────────
RUNBOOK = """\
1. Everything slow + "DB connection pool exhausted" warnings, errors flat, same on every version
   → database saturation. Scale the DB / connection pool. Do NOT roll back: no deploy caused it.
2. Only POST /bookings failing with 502 + "Payment provider timeout" errors
   → payment provider outage. Switch to the backup provider and tell support. Do NOT roll back.
3. Errors start right after a deploy, only on the new version, often one route
   → bad deploy. Roll back to the previous version (ask first), then tell the author."""


@mcp.resource("ops://slo", mime_type="application/json")
def slo() -> dict:
    """The service level objectives the overview checks against."""
    return SLO


@mcp.resource("ops://runbook", mime_type="text/plain")
def runbook() -> str:
    """Known incident patterns and what to do about each."""
    return RUNBOOK


@mcp.resource("ops://status", mime_type="text/plain")
def status() -> str:
    """One-line service status for the last 5 minutes."""
    o = service_overview(5)
    line = (f"{o.as_of} · v{o.version} · {o.requests_per_min} req/min · {o.error_rate_pct}% errors · "
            f"p95 {o.p95_ms} ms · {'; '.join(o.slo_breaches) or 'within SLO'}")
    return f"{line} · WARNING: {o.data_warning}" if o.data_warning else line


# ─────────────── prompts ───────────────
@mcp.prompt(title="Investigate an incident")
def investigate(symptom: str) -> str:
    """Step-by-step incident investigation using the ops tools."""
    return f"""Someone reports: "{symptom}"

Investigate it as the on-call engineer for the StayEase Rentals API, using only the tools.

SLOs: p95 latency ≤ {SLO['p95_ms']} ms, error rate ≤ {SLO['error_rate_pct']}%.

Runbook:
{RUNBOOK}

Steps:
1. service_overview: is it broken, and which routes are worst? Note any data_warning.
2. compare_windows: what changed against the baseline?
3. latency_trend for the worst route: when did it start?
4. error_breakdown and search_logs: what exactly is failing?
5. recent_deploys and errors_by_version: does it line up with a deploy, and only on the new version?
6. Match the evidence to one runbook entry.

Answer in four parts: Impact · Start time · Root cause with evidence · Recommended action.
Quote every number with its time window. Ask me before calling rollback_deploy."""


@mcp.prompt(title="Write an incident report")
def incident_report(minutes: int = 30) -> str:
    """A short incident report for the last N minutes."""
    return f"""Write an incident report for the StayEase Rentals API covering the last {minutes} minutes.

Gather the facts with service_overview, compare_windows, error_breakdown, recent_deploys and errors_by_version.
Every number must come from a tool result; do not estimate or invent any.
Sections: Summary · Impact · Timeline · Root cause · Resolution · Follow-ups. Under 250 words."""


def main() -> None:
    if "--http" in sys.argv:
        mcp.run("streamable-http", port=8000)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
