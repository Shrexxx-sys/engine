"""PERSON B: test the engine alone, no API or dashboard needed.
Run from the project folder:  python -m engine.test_engine"""
import json
from shared.config import PROFILES_PATH
from engine.engine import Engine, score_spo2

profiles = json.load(open(PROFILES_PATH))
eng = Engine(profiles)

# Scale 2 check: 89% is normal for a hypercapnic patient, bad for others
assert score_spo2(89, scale=2) == 0
assert score_spo2(89, scale=1) == 3
print("Scale 1 vs Scale 2 OK")

# Probe removal must give a SENSOR alert, not a clinical one
r = eng.process("p2", "spo2", 0, "2026-11-12T10:00:00+00:00")
print("Probe off ->", r["artifact"], [a["level"] for a in r["alerts"]])
assert r["alerts"][0]["level"] == "SENSOR"

# Very sick readings held for a while -> URGENT
for i in range(10):
    ts = f"2026-11-12T10:{i + 1:02d}:00+00:00"
    for vital, val in [("hr", 135), ("rr", 28), ("spo2", 89), ("temp", 39.5)]:
        r = eng.process("p5", vital, val, ts)
        for a in r["alerts"]:
            print(a["level"], "|", a["rule"], "|", a["profile_fact"])
print("All engine tests passed")
