"""Tests for the clinician dashboard server.  Run from the project folder:
    python -m tools.offline_run              (so monitor.db has the standard test data)
    python -m engine.frontend.test_server

Most tests use a small TEMPORARY database with known rows, so the expected numbers are certain.
The last test checks the real monitor.db against the offline_run table. monitor.db is only read."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from shared import db
from shared.config import DB_PATH
from engine.frontend import server


def make_temp_db(path):
    """Fill a temp DB with the project's own schema (shared/db.py), pointed at a temp file."""
    old = db.DB_PATH
    db.DB_PATH = path
    try:
        db.init()
        start = "2026-11-12T08:%02d:00+00:00"
        # p1: minute 0 normal, minute 1 naive alarm (HR 120), minute 2 SpO2 artifact (probe off)
        db.save_reading("p1", start % 0, "hr", 80, 0, "NONE", None)
        db.save_reading("p1", start % 1, "hr", 120, 2, "NONE", None)
        db.save_reading("p1", start % 2, "spo2", 0, None, None, "probe likely removed (SpO2 = 0)")
        db.save_reading("p1", start % 3, "rr", 26, 7, "URGENT", None)
        # p2: one quiet reading
        db.save_reading("p2", start % 0, "hr", 70, 0, "NONE", None)
        db.save_alerts([
            {"patient_id": "p1", "ts": start % 3, "level": "URGENT", "score": 7, "vital": "rr", "value": 26,
             "rule": "NEWS2-style total 7 [...]. Level rule: total 7 >= 7 -> URGENT", "profile_fact": "<b>fact</b>"},
            {"patient_id": "p1", "ts": start % 2, "level": "SENSOR", "score": None, "vital": "spo2", "value": 0,
             "rule": "Sensor check: probe likely removed", "profile_fact": "-"},
            {"patient_id": "p2", "ts": start % 0, "level": "WATCH", "score": 0, "vital": "hr", "value": 70,
             "rule": "drift", "profile_fact": "-"},
        ])
    finally:
        db.DB_PATH = old


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp, "test.db")
        make_temp_db(cls.db_path)
        cls.hash_before = file_hash(cls.db_path)
        cls.srv = server.make_server("127.0.0.1", 0, db_path=cls.db_path)   # port 0 = any free port
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        # a second server pointed at a database file that does not exist
        cls.srv_empty = server.make_server("127.0.0.1", 0, db_path=os.path.join(cls.tmp, "missing.db"))
        cls.base_empty = f"http://127.0.0.1:{cls.srv_empty.server_address[1]}"
        threading.Thread(target=cls.srv_empty.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        cls.srv_empty.shutdown(); cls.srv_empty.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, base=None):
        try:
            with urllib.request.urlopen((base or self.base) + path, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def get_json(self, path, base=None):
        status, _, body = self.get(path, base)
        return status, json.loads(body)

    # ---------- endpoints
    def test_patients_from_profiles(self):
        status, data = self.get_json("/api/patients")
        self.assertEqual(status, 200)
        ids = [p["id"] for p in data["patients"]]
        self.assertEqual(ids, ["p1", "p2", "p3", "p4", "p5"])
        for p in data["patients"]:
            for key in ("id", "name", "age", "conditions", "medications", "spo2_scale", "on_beta_blocker"):
                self.assertIn(key, p)

    def test_readings_one_patient_oldest_first(self):
        status, data = self.get_json("/api/readings?patient=p1")
        self.assertEqual(status, 200)
        rows = data["readings"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(set(rows[0]), {"id", "patient_id", "ts", "vital", "value", "score", "level", "artifact"})
        self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows))
        self.assertTrue(all(r["patient_id"] == "p1" for r in rows))
        self.assertEqual(rows[2]["artifact"], "probe likely removed (SpO2 = 0)")

    def test_readings_needs_patient_and_unknown_is_empty(self):
        self.assertEqual(self.get_json("/api/readings")[0], 400)
        status, data = self.get_json("/api/readings?patient=zz9")
        self.assertEqual((status, data["readings"]), (200, []))

    def test_alerts_filters(self):
        _, data = self.get_json("/api/alerts")
        self.assertEqual(len(data["alerts"]), 3)
        self.assertGreaterEqual(data["alerts"][0]["ts"], data["alerts"][-1]["ts"])      # newest first
        _, data = self.get_json("/api/alerts?level=SENSOR")
        self.assertEqual([a["level"] for a in data["alerts"]], ["SENSOR"])
        _, data = self.get_json("/api/alerts?level=WATCH,REVIEW,URGENT&patient=p1")
        self.assertEqual([(a["patient_id"], a["level"]) for a in data["alerts"]], [("p1", "URGENT")])
        self.assertEqual(data["alerts"][0]["profile_fact"], "<b>fact</b>")      # sent as plain data
        self.assertEqual(self.get_json("/api/alerts?level=PANIC")[0], 400)

    def test_summary_numbers(self):
        status, s = self.get_json("/api/summary")
        self.assertEqual(status, 200)
        self.assertTrue(s["db_ready"])
        self.assertEqual(s["counts"]["URGENT"], 1)            # p1 latest scored reading is URGENT
        self.assertEqual(s["counts"]["OK"], 1)                # p2
        self.assertEqual(s["counts"]["NO_DATA"], 3)           # p3-p5 have no readings
        p1 = next(p for p in s["patients"] if p["id"] == "p1")
        self.assertEqual((p1["latest_score"], p1["clinical_alerts"], p1["sensor_alerts"]), (7, 1, 1))
        c1 = next(r for r in s["compare"]["patients"] if r["id"] == "p1")
        self.assertEqual(c1["minutes"], 4)
        # naive alarms: HR 120 (min 1), SpO2 0 (min 2), RR 26 (min 3) -> 3, first at minute 1
        self.assertEqual((c1["naive"]["alerts"], c1["naive"]["first_min"]), (3, 1))
        self.assertEqual((c1["ours"]["alerts"], c1["ours"]["first_min"]), (1, 3))
        self.assertEqual(c1["ours"]["per_day"], round(1 / (4 / 1440), 1))
        # all of these happen before deterioration starts (minute 60) -> counted as false
        self.assertEqual((c1["ours"]["false"], c1["naive"]["false"]), (1, 3))
        hours = s["alerts_per_hour"]
        self.assertEqual(hours, [{"hour": "2026-11-12T08:00:00+00:00", "ours": 2, "naive": 3}])
        self.assertEqual(s["compare"]["ward"]["ours"]["alerts"], 2)   # SENSOR never counted

    def test_missing_database_is_friendly(self):
        status, s = self.get_json("/api/summary", self.base_empty)
        self.assertEqual(status, 200)
        self.assertFalse(s["db_ready"])
        self.assertEqual(s["alerts_per_hour"], [])
        status, r = self.get_json("/api/readings?patient=p1", self.base_empty)
        self.assertEqual((status, r["db_ready"], r["readings"]), (200, False, []))
        status, a = self.get_json("/api/alerts", self.base_empty)
        self.assertEqual((status, a["alerts"]), (200, []))

    def test_never_writes_database(self):
        for path in ("/api/patients", "/api/readings?patient=p1", "/api/alerts", "/api/summary"):
            self.get(path)
        self.assertEqual(file_hash(self.db_path), self.hash_before)
        conn = server.open_db(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):     # read-only connection refuses writes
            conn.execute("DELETE FROM alerts")
        conn.close()

    def test_unknown_endpoint_and_bad_fhir_url(self):
        self.assertEqual(self.get_json("/api/nope")[0], 404)
        _, r = self.get_json("/api/fhir?base=ftp://x&what=metadata")
        self.assertFalse(r["ok"])
        _, r = self.get_json("/api/fhir?base=http://127.0.0.1:9/fhir&what=metadata")   # nothing listens on 9
        self.assertEqual((r["ok"], r["error"]), (False, "FHIR server not reachable"))

    # ---------- static files
    def test_static_files_and_disclaimer(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("NOT a medical diagnosis", body.decode("utf-8"))
        for name, ctype in (("/app.js", "javascript"), ("/style.css", "text/css")):
            status, headers, _ = self.get(name)
            self.assertEqual(status, 200, name)
            self.assertIn(ctype, headers["Content-Type"])

    def test_source_and_folders_not_served(self):
        self.assertEqual(self.get("/server.py")[0], 404)
        self.assertEqual(self.get("/../server.py")[0], 404)
        self.assertEqual(self.get("/%2e%2e/server.py")[0], 404)


class RealDatabaseTest(unittest.TestCase):
    """Checks the real monitor.db (made by python -m tools.offline_run) matches the offline_run table."""

    def test_compare_matches_offline_run(self):
        if not os.path.exists(DB_PATH):
            self.skipTest("monitor.db not found - run python -m tools.offline_run first")
        before = file_hash(DB_PATH)
        s = server._compute_summary(DB_PATH, server.PROFILES_PATH, "test")
        got = {r["id"]: (r["ours"]["alerts"], r["naive"]["alerts"], r["ours"]["first_min"], r["naive"]["first_min"])
               for r in s["compare"]["patients"]}
        expected = {"p1": (3, 434, 123, 0), "p2": (0, 0, None, None), "p3": (3, 185, 103, 134),
                    "p4": (0, 21, None, 100), "p5": (0, 0, None, None)}
        self.assertEqual(got, expected)
        self.assertEqual(s["compare"]["ward"]["ours"]["false"], 0)       # target: 0 false alerts
        self.assertEqual(file_hash(DB_PATH), before)                     # untouched


if __name__ == "__main__":
    unittest.main(verbosity=2)
