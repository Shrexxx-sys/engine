"""Clinician dashboard web server - Python standard library only (no Flask, no extra installs).

What it does (simple words):
  1. Serves the website files in engine/frontend/static/ (HTML, CSS, JavaScript).
  2. Answers a few read-only JSON "endpoints" the website asks for every 3 seconds:
       GET /api/patients                  -> the 5 patient profiles (patients/profiles.json)
       GET /api/readings?patient=p1       -> all readings of one patient (oldest first)
       GET /api/alerts?patient=&level=    -> alerts, newest first (level may be "REVIEW" or "WATCH,REVIEW")
       GET /api/summary                   -> ward overview, alerts per hour, our engine vs naive monitor
       GET /api/fhir?base=...&what=metadata|latest -> asks a HAPI FHIR server (for the "How it works" page)
  3. It NEVER writes to monitor.db: the file is opened in SQLite read-only mode (mode=ro).

Run from the project folder:
    python -m tools.offline_run          (fills monitor.db with test data)
    python -m engine.frontend.server     (then open http://localhost:8600)

Clinical decision support only - NOT a medical diagnosis.
"""
import argparse
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shared.config import DB_PATH, PROFILES_PATH

try:
    from simulator.scenarios import DETERIORATION_START   # minute where slow_deterioration begins
except Exception:                                          # keep the website working even if this moves
    DETERIORATION_START = 60

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8600

VITALS = ("hr", "spo2", "rr", "temp")
ALERT_LEVELS = ("WATCH", "REVIEW", "URGENT", "SENSOR")
CLINICAL_LEVELS = ("WATCH", "REVIEW", "URGENT")

# The "naive" monitor we compare against. Copied from tools/offline_run.py naive_alert():
# it alarms on EVERY single reading outside these fixed limits (lo <= value <= hi is normal).
NAIVE_LIMITS = {"hr": (50, 100), "spo2": (92, 101), "rr": (10, 20), "temp": (35.5, 38.0)}

# Which simulated scenario each patient runs. Copied from tools/offline_run.py PLAN.
# Only used on the Compare page to know which alerts were true (deterioration had started) or false.
SCENARIO_PLAN = {"p1": "slow_deterioration", "p2": "stable", "p3": "slow_deterioration",
                 "p4": "artifacts", "p5": "stable"}


# ---------------------------------------------------------------- database (read-only)

def open_db(db_path):
    """Open the SQLite file READ-ONLY. Returns None if the file does not exist yet."""
    if not os.path.exists(db_path):
        return None
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"     # mode=ro -> SQLite refuses every write
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def run_query(db_path, sql, params=()):
    """Run one SELECT. Returns (rows as list of dicts, db_ready True/False). Never raises for a missing DB."""
    conn = open_db(db_path)
    if conn is None:
        return [], False
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows], True
    except sqlite3.OperationalError:          # e.g. tables not created yet, or DB busy
        return [], False
    finally:
        conn.close()


def load_profiles(path=PROFILES_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def get_readings(db_path, patient_id, limit=20000):
    rows, ready = run_query(
        db_path,
        "SELECT * FROM (SELECT id, patient_id, ts, vital, value, score, level, artifact FROM readings "
        "WHERE patient_id=? ORDER BY ts DESC, id DESC LIMIT ?) ORDER BY ts, id",
        (patient_id, limit))
    return rows, ready


def get_alerts(db_path, patient_id=None, levels=None, limit=1000):
    sql = "SELECT id, patient_id, ts, level, score, vital, value, rule, profile_fact FROM alerts WHERE 1=1"
    params = []
    if patient_id:
        sql += " AND patient_id=?"
        params.append(patient_id)
    if levels:
        sql += " AND level IN (%s)" % ",".join("?" * len(levels))
        params.extend(levels)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    return run_query(db_path, sql, params)


# ---------------------------------------------------------------- small helpers

def parse_ts(ts):
    """ISO text -> datetime in UTC (a timestamp without timezone is treated as UTC)."""
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def hour_key(d):
    return d.strftime("%Y-%m-%dT%H:00:00+00:00")


def is_naive_alarm(vital, value):
    if vital not in NAIVE_LIMITS or value is None:
        return False
    lo, hi = NAIVE_LIMITS[vital]
    return not (lo <= value <= hi)


def minute_of(d, first):
    return int((d - first).total_seconds() // 60)


# ---------------------------------------------------------------- summary (ward overview + compare)

_cache = {"key": None, "data": None}
_cache_lock = threading.Lock()


def data_version(db_path):
    """Cheap fingerprint of the database. If it did not change, the website does not need to redraw."""
    r, ready = run_query(db_path, "SELECT (SELECT COUNT(*) FROM readings) AS nr, (SELECT MAX(id) FROM readings) AS mr, "
                                  "(SELECT COUNT(*) FROM alerts) AS na, (SELECT MAX(id) FROM alerts) AS ma")
    if not ready or not r:
        return "empty"
    x = r[0]
    return f"{x['nr']}-{x['mr']}-{x['na']}-{x['ma']}"


def build_summary(db_path=DB_PATH, profiles_path=PROFILES_PATH):
    version = data_version(db_path)
    key = (db_path, profiles_path, version)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["data"]
    data = _compute_summary(db_path, profiles_path, version)
    with _cache_lock:
        _cache["key"], _cache["data"] = key, data
    return data


def _compute_summary(db_path, profiles_path, version):
    profiles = load_profiles(profiles_path)
    readings, ready_r = run_query(db_path, "SELECT patient_id, ts, vital, value, score, level, artifact "
                                           "FROM readings ORDER BY ts, id")
    alerts, ready_a = run_query(db_path, "SELECT patient_id, ts, level FROM alerts ORDER BY ts, id")

    ids = [p.get("id") for p in profiles]
    for r in readings:                                   # a patient in the DB but not in profiles.json
        if r["patient_id"] not in ids:
            ids.append(r["patient_id"])

    per = {pid: {"first": None, "last": None, "latest_score": None, "latest_level": None,
                 "latest_vital": {}, "scores_by_ts": {}, "naive": 0, "naive_first": None,
                 "naive_false": 0, "naive_first_true": None, "urgent_first": None, "readings": 0}
           for pid in ids}
    hours = {}                                          # hour -> {"ours": n, "naive": n}

    for r in readings:
        p = per[r["patient_id"]]
        d = parse_ts(r["ts"])
        if d is None:
            continue
        p["readings"] += 1
        p["first"] = p["first"] or d
        p["last"] = d
        p["latest_vital"][r["vital"]] = r
        if r["score"] is not None:
            p["latest_score"], p["latest_level"] = r["score"], r["level"]
            p["scores_by_ts"][r["ts"]] = r["score"]
        m = minute_of(d, p["first"])
        if r["score"] is not None and r["score"] >= 7 and p["urgent_first"] is None:
            p["urgent_first"] = m
        if is_naive_alarm(r["vital"], r["value"]):
            p["naive"] += 1
            if p["naive_first"] is None:
                p["naive_first"] = m
            if _is_false(r["patient_id"], m):
                p["naive_false"] += 1
            elif p["naive_first_true"] is None and SCENARIO_PLAN.get(r["patient_id"]) == "slow_deterioration":
                p["naive_first_true"] = m
            hours.setdefault(hour_key(d), {"ours": 0, "naive": 0})["naive"] += 1

    ours = {pid: {"alerts": 0, "first": None, "false": 0, "first_true": None, "sensor": 0} for pid in ids}
    for a in alerts:
        pid = a["patient_id"]
        if pid not in ours:
            continue
        if a["level"] == "SENSOR":
            ours[pid]["sensor"] += 1
            continue
        if a["level"] not in CLINICAL_LEVELS:
            continue
        d = parse_ts(a["ts"])
        if d is None:
            continue
        o = ours[pid]
        o["alerts"] += 1
        first = per[pid]["first"] or d
        m = minute_of(d, first)
        if o["first"] is None:
            o["first"] = m
        if _is_false(pid, m):
            o["false"] += 1
        elif o["first_true"] is None and SCENARIO_PLAN.get(pid) == "slow_deterioration":
            o["first_true"] = m
        hours.setdefault(hour_key(d), {"ours": 0, "naive": 0})["ours"] += 1

    # --- ward overview
    patients, counts = [], {"URGENT": 0, "REVIEW": 0, "WATCH": 0, "OK": 0, "NO_DATA": 0}
    for pid in ids:
        p = per[pid]
        if p["latest_level"] is None:
            level = "NO_DATA"
        else:
            level = "OK" if p["latest_level"] == "NONE" else p["latest_level"]
        counts[level] = counts.get(level, 0) + 1
        scores = list(p["scores_by_ts"].values())
        sensor_now = [v for v, r in p["latest_vital"].items() if r["artifact"]]
        patients.append({
            "id": pid, "level": level, "latest_score": p["latest_score"],
            "last_ts": p["last"].isoformat() if p["last"] else None,
            "readings": p["readings"], "sensor_issue_now": sorted(sensor_now),
            "spark": _downsample(scores[-120:], 40),
            "clinical_alerts": ours[pid]["alerts"], "sensor_alerts": ours[pid]["sensor"],
        })

    # --- alerts per hour (fill empty hours with 0 so the bar chart has no holes)
    per_hour = []
    if hours:
        keys = sorted(hours)
        cur, end = parse_ts(keys[0]), parse_ts(keys[-1])
        while cur <= end:
            k = hour_key(cur)
            per_hour.append({"hour": k, **hours.get(k, {"ours": 0, "naive": 0})})
            cur = cur.fromtimestamp(cur.timestamp() + 3600, timezone.utc)

    # --- compare: our engine vs naive monitor
    rows, tot = [], {"minutes": 0, "ours": 0, "naive": 0, "ours_false": 0, "naive_false": 0}
    for pid in ids:
        p, o = per[pid], ours[pid]
        minutes = int((p["last"] - p["first"]).total_seconds() // 60) + 1 if p["first"] else 0
        days = minutes / 1440 if minutes else 0
        known = pid in SCENARIO_PLAN
        rows.append({
            "id": pid, "scenario": SCENARIO_PLAN.get(pid, "unknown"), "minutes": minutes,
            "ours": {"alerts": o["alerts"], "per_day": _rate(o["alerts"], days), "first_min": o["first"],
                     "lead_min": _lead(p["urgent_first"], o["first_true"]),
                     "false": o["false"] if known else None},
            "naive": {"alerts": p["naive"], "per_day": _rate(p["naive"], days), "first_min": p["naive_first"],
                      "lead_min": _lead(p["urgent_first"], p["naive_first_true"]),
                      "false": p["naive_false"] if known else None},
            "urgent_score_min": p["urgent_first"],
        })
        tot["minutes"] += minutes
        tot["ours"] += o["alerts"]
        tot["naive"] += p["naive"]
        tot["ours_false"] += o["false"]
        tot["naive_false"] += p["naive_false"]
    tdays = tot["minutes"] / 1440 if tot["minutes"] else 0
    ward = {"minutes": tot["minutes"],
            "ours": {"alerts": tot["ours"], "per_day": _rate(tot["ours"], tdays), "false": tot["ours_false"]},
            "naive": {"alerts": tot["naive"], "per_day": _rate(tot["naive"], tdays), "false": tot["naive_false"]}}

    return {"db_ready": ready_r and ready_a and bool(readings), "version": version,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "counts": counts, "patients": patients, "alerts_per_hour": per_hour,
            "compare": {"patients": rows, "ward": ward, "deterioration_start_min": DETERIORATION_START,
                        "naive_limits": NAIVE_LIMITS}}


def _is_false(pid, minute):
    """A clinical alert/alarm is FALSE if the patient's simulated scenario has no real deterioration
    (stable, artifacts) or if it fires before deterioration starts (minute < DETERIORATION_START)."""
    scen = SCENARIO_PLAN.get(pid)
    if scen is None:
        return False                      # unknown patient: we cannot judge, never call it false
    return scen != "slow_deterioration" or minute < DETERIORATION_START


def _lead(urgent_min, first_true_min):
    """Detection lead time = minutes between the first TRUE alert and the moment the NEWS2-style score
    itself first reached 7 (urgent). Bigger = earlier warning."""
    if urgent_min is None or first_true_min is None:
        return None
    return urgent_min - first_true_min


def _rate(n, days):
    return round(n / days, 1) if days else None


def _downsample(values, n):
    if len(values) <= n:
        return values
    step = len(values) / n
    return [values[int(i * step)] for i in range(n - 1)] + [values[-1]]


# ---------------------------------------------------------------- FHIR proof (How it works page)

def fhir_fetch(base, what, timeout=4):
    """Ask a HAPI FHIR server for /metadata or the newest Observation. Only http(s), only these 2 paths."""
    u = urllib.parse.urlparse(base or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        return {"ok": False, "error": "Please enter a full http:// or https:// FHIR base URL."}
    base = base.rstrip("/")
    if what == "metadata":
        url = base + "/metadata"
    elif what == "latest":
        url = base + "/Observation?_sort=-date&_count=1"
    else:
        return {"ok": False, "error": "unknown request"}
    req = urllib.request.Request(url, headers={"Accept": "application/fhir+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read(5_000_000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": "FHIR server not reachable", "detail": str(e), "url": url}
    if what == "metadata":
        sw = body.get("software") or {}
        return {"ok": True, "url": url, "resourceType": body.get("resourceType"),
                "fhirVersion": body.get("fhirVersion"), "software": f"{sw.get('name', '?')} {sw.get('version', '')}".strip()}
    entries = body.get("entry") or []
    return {"ok": True, "url": url, "total": body.get("total"),
            "observation": entries[0].get("resource") if entries else None}


# ---------------------------------------------------------------- web server

class Handler(SimpleHTTPRequestHandler):
    db_path = DB_PATH
    profiles_path = PROFILES_PATH
    # Windows sometimes maps .js to text/plain, which browsers refuse to run -> set the types ourselves
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                      ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def list_directory(self, path):                 # never show a file listing
        self.send_error(404, "Not found")
        return None

    def log_message(self, fmt, *args):              # keep the terminal quiet: only show errors
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            self.route_api(url.path, q)
        except Exception as e:                      # never crash the server; tell the website instead
            self.send_json({"error": f"server error: {type(e).__name__}"}, 500)

    def route_api(self, path, q):
        if path == "/api/patients":
            return self.send_json({"patients": load_profiles(self.profiles_path)})
        if path == "/api/readings":
            pid = q.get("patient", "").strip()
            if not pid:
                return self.send_json({"error": "add ?patient=p1"}, 400)
            rows, ready = get_readings(self.db_path, pid)
            return self.send_json({"db_ready": ready, "patient": pid, "readings": rows})
        if path == "/api/alerts":
            levels = [x.strip().upper() for x in q.get("level", "").split(",") if x.strip()]
            bad = [x for x in levels if x not in ALERT_LEVELS]
            if bad:
                return self.send_json({"error": "level must be one of " + ", ".join(ALERT_LEVELS)}, 400)
            rows, ready = get_alerts(self.db_path, q.get("patient", "").strip() or None, levels or None)
            return self.send_json({"db_ready": ready, "alerts": rows})
        if path == "/api/summary":
            return self.send_json(build_summary(self.db_path, self.profiles_path))
        if path == "/api/fhir":
            return self.send_json(fhir_fetch(q.get("base", ""), q.get("what", "")))
        return self.send_json({"error": "unknown endpoint"}, 404)


def make_server(host="127.0.0.1", port=DEFAULT_PORT, db_path=DB_PATH, profiles_path=PROFILES_PATH):
    handler = type("BoundHandler", (Handler,), {"db_path": db_path, "profiles_path": profiles_path})
    return ThreadingHTTPServer((host, port), handler)


def main():
    ap = argparse.ArgumentParser(description="Clinician dashboard (read-only)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = only this computer can open it")
    args = ap.parse_args()
    srv = make_server(args.host, args.port)
    print(f"Clinician dashboard running: http://localhost:{args.port}   (press Ctrl+C to stop)")
    print(f"Reading (read-only): {DB_PATH}")
    print("Clinical decision support only - NOT a medical diagnosis.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
