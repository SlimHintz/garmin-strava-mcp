#!/usr/bin/env python3
"""
Garmin + Strava running-data server.

A bearer-auth REST API plus an MCP endpoint at /tools (FastMCP, streamable HTTP), with a small
SQLite training log. Configuration comes from environment variables; see .env.example.
"""

import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("running-server")

# ==================== APP SETUP ====================

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8787").rstrip("/")
_PUBLIC_HOST = urlparse(PUBLIC_URL).netloc

mcp = FastMCP(
    "Running Data",
    instructions="Access the athlete's Garmin and Strava fitness data. "
                 "Use Garmin tools for running dynamics, training metrics, health data, and splits. "
                 "Use Strava tools for social activity data, segments, and streams.",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_PUBLIC_HOST, "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
    ),
)

CREDS_DIR = Path(os.environ.get("CREDS_DIR", Path(__file__).parent))
STRAVA_CREDS = CREDS_DIR / "strava_creds.json"
GARMIN_CREDS = CREDS_DIR / "garmin_creds.json"
API_KEY = os.environ["API_KEY"]  # from .env (EnvironmentFile); never hardcode

STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"

security = HTTPBearer()

async def verify_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> HTTPAuthorizationCredentials:
    """FastAPI dependency: reject requests whose bearer key doesn't match API_KEY."""
    if not hmac.compare_digest(credentials.credentials, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials

# ==================== RATE LIMITING (REST only) ====================

_request_log: dict[str, list[float]] = {}
RATE_LIMIT = 60

# ==================== STRAVA CLIENT ====================

def load_strava_creds() -> dict:
    """Read the Strava OAuth app + token file."""
    with open(STRAVA_CREDS) as f:
        return json.load(f)

def save_strava_creds(creds: dict) -> None:
    """Write refreshed Strava tokens back to disk."""
    with open(STRAVA_CREDS, "w") as f:
        json.dump(creds, f, indent=2)

def strava_token() -> str:
    """Return a valid Strava access token, refreshing it if it expires within a minute."""
    creds = load_strava_creds()
    if time.time() > creds.get("expires_at", 0) - 60:
        r = httpx.post(STRAVA_TOKEN_URL, data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token"
        })
        r.raise_for_status()
        td = r.json()
        creds["access_token"] = td["access_token"]
        creds["refresh_token"] = td["refresh_token"]
        creds["expires_at"] = td["expires_at"]
        save_strava_creds(creds)
    return creds["access_token"]

def strava_get(endpoint: str, params: Optional[dict] = None) -> Any:
    """GET a Strava API endpoint with the current access token."""
    token = strava_token()
    r = httpx.get(f"{STRAVA_API}{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

# ==================== GARMIN CLIENT ====================

_garmin_client = None

def garmin_client() -> Any:
    """Return a logged-in Garmin Connect client, logging in on first use.

    A login made while the host is overloaded (e.g. just after boot) can come back without a
    display name, which breaks most endpoints. Such a client is not cached, so the next request
    retries the login instead of failing until the process restarts.
    """
    global _garmin_client
    if _garmin_client is None:
        from garminconnect import Garmin
        with open(GARMIN_CREDS) as f:
            creds = json.load(f)
        client = Garmin(creds["email"], creds["password"])
        client.login()
        if not getattr(client, "display_name", None):
            raise HTTPException(status_code=503, detail="Garmin login incomplete; retry shortly")
        _garmin_client = client
    return _garmin_client

def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")

# ==================== MCP TOOLS: GARMIN ====================

@mcp.tool()
def get_garmin_activities(start: int = 0, limit: int = 20) -> list:
    """Get recent Garmin activities with summaries: distance, duration, pace, HR, cadence,
    running dynamics (GCT, stride, vert osc), training effect, training load, VO2 max."""
    return garmin_client().get_activities(start, limit)

@mcp.tool()
def get_garmin_activity_details(activity_id: int) -> dict:
    """Get per-second metric streams for a Garmin activity: speed, HR, cadence, power,
    elevation, ground contact time, vertical oscillation, stride length, respiration, GPS."""
    return garmin_client().get_activity_details(activity_id)

@mcp.tool()
def get_garmin_activity_splits(activity_id: int) -> dict:
    """Get mile/km splits for a Garmin activity including pace, HR, and cadence per split."""
    return garmin_client().get_activity_typed_splits(activity_id)

@mcp.tool()
def get_garmin_training_status(date: Optional[str] = None) -> dict:
    """Get Garmin training status: VO2 max estimate, training load, fitness level,
    and training load focus. Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_training_status(date or today())

@mcp.tool()
def get_garmin_race_predictions() -> dict:
    """Get Garmin predicted race finish times for 5K, 10K, half marathon, and marathon
    based on current fitness."""
    return garmin_client().get_race_predictions()

@mcp.tool()
def get_garmin_lactate_threshold() -> dict:
    """Get Garmin lactate threshold heart rate and pace."""
    return garmin_client().get_lactate_threshold()

@mcp.tool()
def get_garmin_personal_records() -> list:
    """Get Garmin personal records (PRs) for various distances."""
    return garmin_client().get_personal_record()

@mcp.tool()
def get_garmin_sleep(date: Optional[str] = None) -> dict:
    """Get Garmin sleep data: total sleep, sleep stages, sleep score.
    Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_sleep_data(date or today())

@mcp.tool()
def get_garmin_hrv(date: Optional[str] = None) -> dict:
    """Get Garmin HRV (heart rate variability) data.
    Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_hrv_data(date or today())

@mcp.tool()
def get_garmin_body_battery(date: Optional[str] = None) -> list:
    """Get Garmin body battery charge level throughout the day.
    Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_body_battery(date or today())

@mcp.tool()
def get_garmin_training_readiness(date: Optional[str] = None) -> dict:
    """Get Garmin training readiness score, factoring in sleep, HRV, recovery, and load.
    Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_training_readiness(date or today())

@mcp.tool()
def get_garmin_heart_rate(date: Optional[str] = None) -> dict:
    """Get Garmin daily heart rate: resting HR, max HR, HR throughout the day.
    Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_heart_rates(date or today())

@mcp.tool()
def get_garmin_stress(date: Optional[str] = None) -> dict:
    """Get Garmin stress levels throughout the day.
    Date defaults to today (YYYY-MM-DD)."""
    return garmin_client().get_stress_data(date or today())

# ==================== MCP TOOLS: STRAVA ====================

@mcp.tool()
def get_strava_activities(
    per_page: int = 30,
    page: int = 1,
    after: Optional[int] = None,
    before: Optional[int] = None
) -> list:
    """Get Strava activities with distance, pace, HR, elevation, kudos.
    Filter by unix timestamps with after/before."""
    params = {"per_page": per_page, "page": page}
    if after: params["after"] = after
    if before: params["before"] = before
    return strava_get("/athlete/activities", params)

@mcp.tool()
def get_strava_activity(activity_id: int) -> dict:
    """Get full details for a single Strava activity including segment efforts and best efforts."""
    return strava_get(f"/activities/{activity_id}", {"include_all_efforts": True})

@mcp.tool()
def get_strava_activity_streams(
    activity_id: int,
    keys: str = "time,distance,heartrate,velocity_smooth,cadence,altitude"
) -> dict:
    """Get time-series data streams for a Strava activity.
    Available keys: time, distance, heartrate, velocity_smooth, cadence, altitude, watts, temp."""
    return strava_get(f"/activities/{activity_id}/streams", {"keys": keys, "key_by_type": True})

@mcp.tool()
def get_strava_activity_laps(activity_id: int) -> list:
    """Get lap splits for a Strava activity."""
    return strava_get(f"/activities/{activity_id}/laps")

@mcp.tool()
def get_strava_stats() -> dict:
    """Get all-time Strava athlete stats: total distance, time, and elevation for runs, rides, swims."""
    athlete = strava_get("/athlete")
    return strava_get(f"/athletes/{athlete['id']}/stats")

@mcp.tool()
def get_strava_athlete() -> dict:
    """Get Strava athlete profile: name, location, follower count, and gear."""
    return strava_get("/athlete")

# ==================== REST ROUTES (legacy, bearer-auth) ====================


# ==================== TRAINING DATABASE TOOLS ====================

_DB_PATH = os.environ.get("TRAINING_DB_PATH", str(Path(__file__).parent / "training.db"))
_SCHEMA = Path(__file__).parent / "training_schema.sql"


def _db() -> sqlite3.Connection:
    """Open the training-log database, creating its tables on first use."""
    conn = sqlite3.connect(_DB_PATH)
    conn.executescript(_SCHEMA.read_text())
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_pace(sec: Optional[float]) -> Optional[str]:
    """Seconds per mile -> 'M:SS'."""
    if sec is None:
        return None
    return f"{int(sec)//60}:{int(sec)%60:02d}"


@mcp.tool()
def log_run(
    activity_id: str,
    week: int,
    phase: str,
    date: str,
    name: str,
    distance_mi: float,
    moving_time_sec: int,
    avg_hr: int,
    max_hr: int,
    avg_pace_sec_per_mi: int,
    training_load: Optional[float] = None,
    training_effect_label: Optional[str] = None,
    vo2max: Optional[float] = None,
    shoe: Optional[str] = None,
    weather_temp: Optional[float] = None,
    aqi: Optional[int] = None,
    notes: Optional[str] = None,
    avg_gct_ms: Optional[float] = None,
    avg_cadence: Optional[float] = None,
    avg_stride_cm: Optional[float] = None,
    avg_vertical_osc_cm: Optional[float] = None,
    avg_vertical_ratio: Optional[float] = None,
    avg_respiration: Optional[float] = None,
) -> dict:
    """Log a completed run to the training database. Upserts into runs and session_metrics; records form_trends."""
    try:
        with _db() as conn:
            conn.execute("""
                INSERT INTO runs (
                    activity_id, date, week, phase, name, distance_mi,
                    moving_time_sec, avg_hr, max_hr, avg_pace_sec_per_mi,
                    training_load, training_effect_label, vo2max, shoe,
                    weather_temp, aqi, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    date=excluded.date, week=excluded.week, phase=excluded.phase,
                    name=excluded.name, distance_mi=excluded.distance_mi,
                    moving_time_sec=excluded.moving_time_sec,
                    avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
                    avg_pace_sec_per_mi=excluded.avg_pace_sec_per_mi,
                    training_load=excluded.training_load,
                    training_effect_label=excluded.training_effect_label,
                    vo2max=excluded.vo2max, shoe=excluded.shoe,
                    weather_temp=excluded.weather_temp, aqi=excluded.aqi,
                    notes=excluded.notes
            """, (activity_id, date, week, phase, name, distance_mi,
                  moving_time_sec, avg_hr, max_hr, avg_pace_sec_per_mi,
                  training_load, training_effect_label, vo2max, shoe,
                  weather_temp, aqi, notes))

            conn.execute("""
                INSERT INTO session_metrics (
                    activity_id, avg_gct_ms, avg_cadence, avg_stride_cm,
                    avg_vertical_osc_cm, avg_vertical_ratio, avg_respiration
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    avg_gct_ms=excluded.avg_gct_ms,
                    avg_cadence=excluded.avg_cadence,
                    avg_stride_cm=excluded.avg_stride_cm,
                    avg_vertical_osc_cm=excluded.avg_vertical_osc_cm,
                    avg_vertical_ratio=excluded.avg_vertical_ratio,
                    avg_respiration=excluded.avg_respiration
            """, (activity_id, avg_gct_ms, avg_cadence, avg_stride_cm,
                  avg_vertical_osc_cm, avg_vertical_ratio, avg_respiration))

            conn.execute("DELETE FROM form_trends WHERE activity_id=?", (activity_id,))
            conn.execute("""
                INSERT INTO form_trends (
                    activity_id, date, week, session_type,
                    avg_gct_ms, avg_cadence, avg_stride_cm,
                    avg_vertical_osc_cm, avg_hr, avg_pace_sec_per_mi
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (activity_id, date, week, training_effect_label,
                  avg_gct_ms, avg_cadence, avg_stride_cm,
                  avg_vertical_osc_cm, avg_hr, avg_pace_sec_per_mi))

        return {
            "status": "logged",
            "activity_id": activity_id,
            "date": date,
            "week": week,
            "phase": phase,
            "distance_mi": round(distance_mi, 2),
            "pace": _fmt_pace(avg_pace_sec_per_mi),
            "avg_hr": avg_hr,
        }
    except Exception as e:
        log.error(f"log_run error: {e}")
        return {"error": str(e)}


@mcp.tool()
def log_intervals(activity_id: str, intervals: list) -> dict:
    """Log structured interval data for a quality session. Replaces any existing intervals for the activity."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM intervals WHERE activity_id=?", (activity_id,))
            conn.executemany("""
                INSERT INTO intervals (
                    activity_id, interval_type, interval_number,
                    distance_mi, time_sec, avg_pace_actual_sec_per_mi,
                    avg_hr, max_hr, avg_gct_ms, avg_cadence, avg_stride_cm
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, [(
                activity_id,
                iv.get("interval_type"),
                iv.get("interval_number"),
                iv.get("distance_mi"),
                iv.get("time_sec"),
                iv.get("avg_pace_actual_sec_per_mi"),
                iv.get("avg_hr"),
                iv.get("max_hr"),
                iv.get("avg_gct_ms"),
                iv.get("avg_cadence"),
                iv.get("avg_stride_cm"),
            ) for iv in intervals])

        active = [iv for iv in intervals if iv.get("interval_type") == "active"]
        return {
            "status": "logged",
            "activity_id": activity_id,
            "total_intervals": len(intervals),
            "active_intervals": len(active),
            "active_paces": [_fmt_pace(iv.get("avg_pace_actual_sec_per_mi")) for iv in active],
        }
    except Exception as e:
        log.error(f"log_intervals error: {e}")
        return {"error": str(e)}


@mcp.tool()
def get_training_log(
    weeks: Optional[int] = 4,
    week_number: Optional[int] = None,
    session_type: Optional[str] = None,
) -> list:
    """Retrieve training log entries. Joins runs + session_metrics, newest first. Pace formatted as MM:SS/mi."""
    try:
        with _db() as conn:
            where_clauses = []
            params: list = []

            if week_number is not None:
                where_clauses.append("r.week = ?")
                params.append(week_number)
            elif weeks is not None:
                max_week = conn.execute("SELECT MAX(week) FROM runs").fetchone()[0] or 0
                where_clauses.append("r.week >= ?")
                params.append(max_week - weeks + 1)

            if session_type:
                where_clauses.append("r.training_effect_label = ?")
                params.append(session_type)

            where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            rows = conn.execute(f"""
                SELECT r.*, sm.avg_gct_ms, sm.avg_cadence, sm.avg_stride_cm,
                       sm.avg_vertical_osc_cm, sm.avg_vertical_ratio, sm.avg_respiration
                FROM runs r
                LEFT JOIN session_metrics sm ON r.activity_id = sm.activity_id
                {where}
                ORDER BY r.date DESC
            """, params).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            d["pace_formatted"] = _fmt_pace(d.get("avg_pace_sec_per_mi"))
            result.append(d)
        return result
    except Exception as e:
        log.error(f"get_training_log error: {e}")
        return [{"error": str(e)}]


@mcp.tool()
def update_weekly_summary(
    week: int,
    phase: Optional[str] = None,
    target_miles: Optional[float] = None,
    actual_miles: Optional[float] = None,
    tue_weight: Optional[float] = None,
    notes: Optional[str] = None,
) -> dict:
    """Update or insert a weekly training summary. Auto-calculates actual_miles from runs table if not provided."""
    try:
        with _db() as conn:
            if actual_miles is None:
                row = conn.execute(
                    "SELECT ROUND(SUM(distance_mi), 2) FROM runs WHERE week=?", (week,)
                ).fetchone()
                actual_miles = row[0] or 0.0

            existing = conn.execute(
                "SELECT * FROM weekly_summary WHERE week=?", (week,)
            ).fetchone()
            if existing:
                existing = dict(existing)
                phase = phase if phase is not None else existing.get("phase")
                target_miles = target_miles if target_miles is not None else existing.get("target_miles")
                tue_weight = tue_weight if tue_weight is not None else existing.get("tue_weight")
                notes = notes if notes is not None else existing.get("notes")

            conn.execute("""
                INSERT INTO weekly_summary (week, phase, target_miles, actual_miles, tue_weight, notes, updated_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(week) DO UPDATE SET
                    phase=excluded.phase,
                    target_miles=excluded.target_miles,
                    actual_miles=excluded.actual_miles,
                    tue_weight=excluded.tue_weight,
                    notes=excluded.notes,
                    updated_at=CURRENT_TIMESTAMP
            """, (week, phase, target_miles, actual_miles, tue_weight, notes))

            row = conn.execute("SELECT * FROM weekly_summary WHERE week=?", (week,)).fetchone()
            return dict(row)
    except Exception as e:
        log.error(f"update_weekly_summary error: {e}")
        return {"error": str(e)}


@mcp.tool()
def get_weekly_summary() -> list:
    """Return all weekly summaries with actual vs target mileage, delta, and run count per week."""
    try:
        with _db() as conn:
            rows = conn.execute("""
                SELECT ws.*,
                       COUNT(r.id) AS run_count,
                       ROUND(COALESCE(ws.actual_miles, 0) - COALESCE(ws.target_miles, 0), 2) AS delta
                FROM weekly_summary ws
                LEFT JOIN runs r ON ws.week = r.week
                GROUP BY ws.week
                ORDER BY ws.week
            """).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"get_weekly_summary error: {e}")
        return [{"error": str(e)}]


@mcp.tool()
def log_checkpoint(
    date: str,
    checkpoint_name: str,
    seg1_pace: Optional[str] = None,
    seg1_hr: Optional[int] = None,
    seg2_pace: Optional[str] = None,
    seg2_hr: Optional[int] = None,
    seg3_pace: Optional[str] = None,
    seg3_hr: Optional[int] = None,
    outcome: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Log checkpoint test results (CP1/CP2/CP3) with per-segment pace and HR."""
    try:
        with _db() as conn:
            cursor = conn.execute("""
                INSERT INTO checkpoints (
                    date, checkpoint_name,
                    seg1_pace, seg1_hr, seg2_pace, seg2_hr,
                    seg3_pace, seg3_hr, outcome, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (date, checkpoint_name, seg1_pace, seg1_hr, seg2_pace, seg2_hr,
                  seg3_pace, seg3_hr, outcome, notes))
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return {"status": "logged", "checkpoint": dict(row)}
    except Exception as e:
        log.error(f"log_checkpoint error: {e}")
        return {"error": str(e)}


@mcp.tool()
def get_form_trends(
    weeks: Optional[int] = 8,
    session_type: Optional[str] = None,
) -> dict:
    """Return form metric trends (GCT, cadence, stride, vert osc) with 3-run rolling averages."""
    try:
        with _db() as conn:
            max_week = conn.execute("SELECT MAX(week) FROM form_trends").fetchone()[0] or 0
            where_clauses = ["ft.week >= ?"]
            params: list = [max_week - (weeks or 8) + 1]

            if session_type:
                where_clauses.append("ft.session_type = ?")
                params.append(session_type)

            rows = conn.execute(f"""
                SELECT ft.*
                FROM form_trends ft
                WHERE {" AND ".join(where_clauses)}
                ORDER BY ft.date ASC
            """, params).fetchall()

        data = [dict(row) for row in rows]

        rolling_keys = ["avg_gct_ms", "avg_cadence", "avg_stride_cm", "avg_vertical_osc_cm"]
        for i, row in enumerate(data):
            start = max(0, i - 2)
            for key in rolling_keys:
                vals = [r[key] for r in data[start:i + 1] if r.get(key) is not None]
                row[f"{key}_r3"] = round(sum(vals) / len(vals), 1) if vals else None
            row["pace_formatted"] = _fmt_pace(row.get("avg_pace_sec_per_mi"))

        return {"runs": data, "count": len(data)}
    except Exception as e:
        log.error(f"get_form_trends error: {e}")
        return {"error": str(e)}


@mcp.tool()
def get_checkpoint_history() -> list:
    """Return all checkpoint test results for the training block, ordered by date."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints ORDER BY date ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"get_checkpoint_history error: {e}")
        return [{"error": str(e)}]


# ==================== HEALTH + MCP MOUNT ====================

# Initialize FastMCP app AFTER all tools are registered so the session manager
# captures the full tool list. The lifespan will start its internal task group.
mcp.settings.streamable_http_path = "/"
mcp.settings.json_response = True
_mcp_app = mcp.streamable_http_app()

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 (async context manager)
    async with _mcp_app.router.lifespan_context(_mcp_app):
        yield

app = FastAPI(title="Running Data Server", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def rate_limit(request: Request, call_next: Any) -> Any:
    """Per-IP rate limit for the REST API (MCP and health checks are exempt)."""
    # Rewrite /tools (no slash) to /tools/ so MCP clients don't get a redirect
    if request.scope.get("path") == "/tools":
        request.scope["path"] = "/tools/"
        request.scope["raw_path"] = b"/tools/"
    if request.url.path.startswith("/tools") or request.url.path == "/":
        return await call_next(request)
    now = time.time()
    ip = request.client.host
    _request_log.setdefault(ip, [])
    _request_log[ip] = [t for t in _request_log[ip] if now - t < 60]
    if len(_request_log[ip]) >= RATE_LIMIT:
        return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    _request_log[ip].append(now)
    return await call_next(request)

@app.get("/")
def health() -> dict:
    return {"status": "ok"}

@app.get("/strava/athlete")
def rest_strava_athlete(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return strava_get("/athlete")

@app.get("/strava/activities")
def rest_strava_activities(per_page: int = 30, page: int = 1, after: Optional[int] = None, before: Optional[int] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    params = {"per_page": per_page, "page": page}
    if after: params["after"] = after
    if before: params["before"] = before
    return strava_get("/athlete/activities", params)

@app.get("/strava/activities/all")
def rest_strava_activities_all(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    all_acts, page = [], 1
    while True:
        batch = strava_get("/athlete/activities", {"per_page": 200, "page": page})
        if not batch:
            break
        all_acts.extend(batch)
        page += 1
    return all_acts

@app.get("/strava/activity/{activity_id}")
def rest_strava_activity(activity_id: int, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return strava_get(f"/activities/{activity_id}", {"include_all_efforts": True})

@app.get("/strava/activity/{activity_id}/streams")
def rest_strava_streams(activity_id: int, keys: str = "time,distance,heartrate,velocity_smooth,cadence,altitude", auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return strava_get(f"/activities/{activity_id}/streams", {"keys": keys, "key_by_type": True})

@app.get("/strava/activity/{activity_id}/laps")
def rest_strava_laps(activity_id: int, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return strava_get(f"/activities/{activity_id}/laps")

@app.get("/strava/stats")
def rest_strava_stats(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    athlete = strava_get("/athlete")
    return strava_get(f"/athletes/{athlete['id']}/stats")

@app.get("/garmin/activities")
def rest_garmin_activities(start: int = 0, limit: int = 100, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_activities(start, limit)

@app.get("/garmin/activities/all")
def rest_garmin_activities_all(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    gc, all_acts, start = garmin_client(), [], 0
    while True:
        batch = gc.get_activities(start, 100)
        if not batch:
            break
        all_acts.extend(batch)
        start += 100
    return all_acts

@app.get("/garmin/activity/{activity_id}")
def rest_garmin_activity(activity_id: int, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_activity(activity_id)

@app.get("/garmin/activity/{activity_id}/details")
def rest_garmin_details(activity_id: int, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_activity_details(activity_id)

@app.get("/garmin/activity/{activity_id}/splits")
def rest_garmin_splits(activity_id: int, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_activity_typed_splits(activity_id)

@app.get("/garmin/stats")
def rest_garmin_stats(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_stats(date or today())

@app.get("/garmin/training-status")
def rest_garmin_training_status(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_training_status(date or today())

@app.get("/garmin/race-predictions")
def rest_garmin_race_predictions(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_race_predictions()

@app.get("/garmin/lactate-threshold")
def rest_garmin_lactate_threshold(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_lactate_threshold()

@app.get("/garmin/personal-records")
def rest_garmin_personal_records(auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_personal_record()

@app.get("/garmin/sleep")
def rest_garmin_sleep(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_sleep_data(date or today())

@app.get("/garmin/hrv")
def rest_garmin_hrv(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_hrv_data(date or today())

@app.get("/garmin/body-battery")
def rest_garmin_body_battery(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_body_battery(date or today())

@app.get("/garmin/training-readiness")
def rest_garmin_training_readiness(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_training_readiness(date or today())

@app.get("/garmin/heart-rate")
def rest_garmin_hr(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_heart_rates(date or today())

@app.get("/garmin/stress")
def rest_garmin_stress(date: Optional[str] = None, auth: HTTPAuthorizationCredentials = Depends(verify_key)) -> Any:
    return garmin_client().get_stress_data(date or today())

app.mount("/tools", _mcp_app)

import oauth_guard  # noqa: E402  (auth for /tools + OAuth for remote MCP clients)
oauth_guard.install(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8787")))
