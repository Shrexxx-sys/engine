"""PERSON B: test the engine alone, no API or dashboard needed.
Run from the project folder:  python -m engine.test_engine"""
import json
from datetime import datetime, timedelta, timezone
from shared.config import PROFILES_PATH
from engine.engine import Engine, score_spo2
from simulator.scenarios import generate      # read-only use of Person C's generator

profiles = json.load(open(PROFILES_PATH))
eng = Engine(profiles)
START = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
WOBBLE = [0, 1, -1, 2, -2, 1]                               # small natural variation, so it is not a "flatline"


def normal(m, **override):
    """Normal vitals for minute m with a little wobble; override any vital, e.g. normal(5, hr=130)."""
    w = WOBBLE[m % len(WOBBLE)]
    v = {"hr": 76 + w, "spo2": 97 + (w > 0), "rr": 15 + (w > 1), "temp": 36.8}
    v.update(override)
    return v


def run(engine, pid, minutes_of_vitals):
    """Feed a list of {vital: value} dicts (one per simulated minute). Returns (clinical, sensor, results)."""
    clinical, sensor, results = [], [], []
    for m, vitals in enumerate(minutes_of_vitals):
        ts = (START + timedelta(minutes=m)).isoformat()
        for vital, value in vitals.items():
            r = engine.process(pid, vital, value, ts)
            results.append((m, vital, value, r))
            for a in r["alerts"]:
                (sensor if a["level"] == "SENSOR" else clinical).append(a)
    return clinical, sensor, results


# Scale 2 check: 89% is normal for a hypercapnic patient, bad for others
assert score_spo2(89, scale=2) == 0
assert score_spo2(89, scale=1) == 3
print("Scale 1 vs Scale 2 OK")

# Probe removal must give a SENSOR alert, not a clinical one
r = eng.process("p2", "spo2", 0, "2026-11-12T10:00:00+00:00")
print("Probe off ->", r["artifact"], [a["level"] for a in r["alerts"]])
assert r["alerts"][0]["level"] == "SENSOR"

# Very sick readings held for a while -> URGENT
levels = []
for i in range(10):
    ts = f"2026-11-12T10:{i + 1:02d}:00+00:00"
    for vital, val in [("hr", 135), ("rr", 28), ("spo2", 89), ("temp", 39.5)]:
        r = eng.process("p5", vital, val, ts)
        for a in r["alerts"]:
            levels.append(a["level"])
            print(a["level"], "|", a["rule"], "|", a["profile_fact"])
assert "URGENT" in levels, levels
print("Sustained very sick readings -> URGENT OK")

# B1: a 2-minute HR spike that goes back to normal -> no clinical alert, only ONE sensor alert
data = [normal(m, hr=131) if m in (40, 41) else normal(m) for m in range(62)]
clinical, sensor, results = run(Engine(profiles), "p2", data)
assert clinical == [], clinical
assert len(sensor) == 1 and sensor[0]["vital"] == "hr" and "sudden jump" in sensor[0]["rule"], sensor
assert any(r["artifact"] and r["artifact"].startswith("sudden jump") for m, v, x, r in results if m == 40)
print("Spike that returns -> 1 SENSOR alert, no clinical alert OK:", sensor[0]["rule"])

# B1: a REAL sudden rise that stays -> held for 2 readings, then accepted and alerted
data = [normal(m, hr=135 + m % 2) if m >= 40 else normal(m) for m in range(50)]
clinical, sensor, results = run(Engine(profiles), "p2", data)
assert len(clinical) >= 1 and clinical[0]["vital"] == "hr", clinical
first_scored = min(m for m, v, x, r in results if v == "hr" and x >= 135 and r["artifact"] is None)
assert first_scored == 42, first_scored                     # minute 40, 41 held; 42 confirms
print("Sustained real rise -> clinical alert OK:", clinical[0]["level"], "|", clinical[0]["rule"])

# B1: motion noise (HR and RR jumping around) -> no clinical alert, at most ONE sensor alert per vital
HR_NOISE = [20, -24, 12, -18, 26, -10, 22, -26, 8, -20, 24, -14, 18, -22, 10]
RR_NOISE = [7, -6, 4, -7, 11, -5, 9, -8, 3, -6, 13, -4, 6, -7, 5]
data = [normal(m) for m in range(40)]
data += [normal(40 + i, hr=76 + h, rr=15 + r) for i, (h, r) in enumerate(zip(HR_NOISE, RR_NOISE))]
data += [normal(m) for m in range(55, 75)]
clinical, sensor, results = run(Engine(profiles), "p2", data)
assert clinical == [], clinical
for vital in ("hr", "rr"):
    assert len([a for a in sensor if a["vital"] == vital]) <= 1, sensor
assert any(r["artifact"] and r["artifact"].startswith("motion noise") for m, v, x, r in results if v == "rr")
assert all(r["artifact"] is None for m, v, x, r in results if m >= 65)     # back to normal once calm
print("Motion noise -> SENSOR only OK:", [a["rule"] for a in sensor])

# B1: the full simulated "artifacts" scenario (p4) -> no clinical alert, ONE sensor alert per vital per episode
p4 = next(p for p in profiles if p["id"] == "p4")
data = [{k: x for k, x in v.items() if x is not None} for ts, v in generate("artifacts", 240, p4)]
clinical, sensor, results = run(Engine(profiles), "p4", data)
assert clinical == [], clinical
for lo, hi in [(100, 110), (150, 160), (200, 225)]:              # probe off, spike, motion noise
    episode = [a["vital"] for a in sensor if lo <= (datetime.fromisoformat(a["ts"]) - START).seconds // 60 < hi]
    assert len(episode) == len(set(episode)), (lo, episode)       # no vital alerted twice in one episode
print("Artifacts scenario -> 0 clinical alerts, 1 SENSOR alert per vital per episode OK")

print("All engine tests passed")
