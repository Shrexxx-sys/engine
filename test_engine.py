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


def run(engine, pid, minutes_of_vitals, step_seconds=60):
    """Feed a list of {vital: value} dicts, one every step_seconds (60 = simulator, 1 = BIDMC 1 Hz).
    Returns (clinical, sensor, results); results = (index, vital, value, engine result)."""
    clinical, sensor, results = [], [], []
    for m, vitals in enumerate(minutes_of_vitals):
        ts = (START + timedelta(seconds=m * step_seconds)).isoformat()
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

# B2: persistence is measured in TIME (reading timestamps), not in number of readings
def sick(i):
    return {"hr": 135 + i % 2, "rr": 28, "spo2": 89, "temp": 39.5}


clinical, _, _ = run(Engine(profiles), "p5", [sick(0)] + [normal(m) for m in range(1, 30)])
assert clinical == [], clinical
print("B2: 1 minute of bad readings -> no alert OK")

clinical, _, _ = run(Engine(profiles), "p5", [sick(i) for i in range(30)], step_seconds=1)
assert clinical == [], clinical                              # 120 readings in 30 s is still not 2 minutes
print("B2: 30 s of bad readings at 1 Hz (120 readings) -> no alert OK")

clinical, _, _ = run(Engine(profiles), "p5", [sick(i) for i in range(180)], step_seconds=1)
assert len(clinical) == 1 and clinical[0]["level"] == "URGENT", clinical
secs = (datetime.fromisoformat(clinical[0]["ts"]) - START).total_seconds()
assert 115 <= secs <= 125, secs                              # alert after ~2 minutes of clock time
print(f"B2: 3 min of bad readings at 1 Hz -> one URGENT after {secs:.0f} s OK")

# escalation only + reset after 20 calm minutes
data = ([sick(i) for i in range(10)] + [normal(m) for m in range(10, 20)]     # episode 1, calm 10 min
        + [sick(i) for i in range(10)] + [normal(m) for m in range(40, 65)]  # episode 2 (too soon), calm 25 min
        + [sick(i) for i in range(10)])                                      # episode 3 -> alerts again
clinical, _, _ = run(Engine(profiles), "p2", data)
mins = [(datetime.fromisoformat(a["ts"]) - START).seconds // 60 for a in clinical]
assert [a["level"] for a in clinical] == ["URGENT", "URGENT"], clinical
assert mins[0] < 10 and mins[1] >= 55, mins
print("B2: one alert per episode, re-alert only after 20 calm minutes OK, alerts at minutes", mins)

# B3: flatline is judged over TIME (>= 5 min) AND readings (>= 10)
def flat_alerts(sensor):
    return [a for a in sensor if "flat" in a["rule"]]


# BIDMC-like 1 Hz data: HR repeats the same integer for 7 seconds at a time, for 10 minutes -> NOT a flatline
data = [{"hr": 80 + (i // 7) % 3 - 1, "spo2": 97 + (i // 11) % 2, "rr": 15} for i in range(600)]
clinical, sensor, results = run(Engine(profiles), "p2", data, step_seconds=1)
assert sensor == [] and clinical == [], (sensor, clinical)
print("B3: 1 Hz HR repeating for a few seconds (600 readings) -> no flatline OK")

# 1 Hz: HR exactly the same for 60 s = 60 readings but only 1 minute -> NOT a flatline yet
data = [{"hr": 80, "spo2": 97 + (i // 11) % 2, "rr": 15} for i in range(60)]
clinical, sensor, results = run(Engine(profiles), "p2", data, step_seconds=1)
assert flat_alerts(sensor) == [], sensor
print("B3: 1 Hz HR identical for 60 s -> no flatline OK")

# 1 Hz: HR stuck for 6 minutes -> ONE sensor alert, after about 5 minutes
data = [{"hr": 80, "spo2": 97 + (i // 11) % 2, "rr": 15} for i in range(360)]
clinical, sensor, results = run(Engine(profiles), "p2", data, step_seconds=1)
assert len(sensor) == 1 and "flat" in sensor[0]["rule"] and clinical == [], (sensor, clinical)
assert (datetime.fromisoformat(sensor[0]["ts"]) - START).total_seconds() == 300, sensor[0]["ts"]
print("B3: 1 Hz HR stuck 6 min -> 1 SENSOR alert at 5 min OK:", sensor[0]["rule"][:90])

# simulator rate (1/min): HR identical for 6 readings (5 min) -> NOT a flatline (too few readings)
data = [normal(m) for m in range(40)] + [normal(m, hr=77) for m in range(40, 46)] + [normal(m) for m in range(46, 60)]
clinical, sensor, results = run(Engine(profiles), "p2", data)
assert flat_alerts(sensor) == [], sensor
print("B3: 1/min HR identical for 6 readings -> no flatline OK")

# probe removal: SpO2 0 + HR flat, while RR is truly high -> ONE probe alert, NO clinical alert while it lasts
data = ([normal(m) for m in range(40)]
        + [normal(m, spo2=0, hr=80, rr=26 + m % 2) for m in range(40, 52)]      # probe off, RR 26-27 (3 pts)
        + [normal(m, rr=26 + m % 2) for m in range(52, 60)])                     # probe back, still unwell
clinical, sensor, results = run(Engine(profiles), "p2", data)
probe = [a for a in sensor if "probe" in a["rule"]]
assert len(probe) == 1 and probe[0]["ts"] == (START + timedelta(minutes=40)).isoformat(), probe
during = [a for a in clinical if (datetime.fromisoformat(a["ts"]) - START).seconds // 60 < 52]
assert during == [], during
assert any(r["level"] == "REVIEW" for m, v, x, r in results if 44 <= m < 52)   # it WOULD alert: paused only
assert clinical, "real problem must still alert once the probe is back"
print("B3: probe removal -> 1 SENSOR alert, no clinical alert while off, alert after OK:", probe[0]["rule"][:70])

# B4: data gaps -> ONE "no data" SENSOR alert per gap, never a clinical alert
def gap_alerts(alerts):
    return [a for a in alerts if a["rule"].startswith("No data received")]


def at(minute):
    return (START + timedelta(minutes=minute)).isoformat()


data = [normal(m) for m in range(20)] + [{}] * 10 + [normal(m) for m in range(30, 40)]   # minutes 20-29: nothing (last reading 19, next 30 = 11 min)
clinical, sensor, results = run(Engine(profiles), "p2", data)
gaps = gap_alerts(sensor)
assert len(gaps) == 1 and gaps[0]["ts"] == at(30) and "for 11 minutes" in gaps[0]["rule"], gaps
assert gaps[0]["level"] == "SENSOR" and clinical == [], clinical
print("B4: 11-minute gap -> 1 SENSOR alert:", gaps[0]["rule"])

data = [normal(m) for m in range(20)] + [{}] * 4 + [normal(m) for m in range(24, 40)]     # 5 min apart = not > 5
clinical, sensor, results = run(Engine(profiles), "p2", data)
assert gap_alerts(sensor) == [], sensor
print("B4: 5-minute gap (not more than 5) -> no alert OK")

e = Engine(profiles)
run(e, "p2", [normal(m) for m in range(20)])                 # last reading at minute 19
assert e.check_missing(at(23)) == []                         # 4 min: fine
missing = e.check_missing(at(25))                            # 6 min: alert
assert len(missing) == 1 and missing[0]["patient_id"] == "p2" and "for 6 minutes" in missing[0]["rule"], missing
assert e.check_missing(at(25.5)) == [] and e.check_missing(at(28)) == []        # same gap: no repeats
r = e.process("p2", "hr", 77, at(30))
assert gap_alerts(r["alerts"]) == [], r["alerts"]            # data comes back: no 2nd alert for the same gap
assert e.check_missing(at(34)) == []                         # fresh data -> nothing missing
assert len(e.check_missing(at(36))) == 1                     # a NEW gap -> a new alert
print("B4: check_missing -> 1 alert per gap, no duplicates with process() OK")

# a gap breaks "held continuously": 1 bad minute, 10 min silence, 1 bad minute -> no clinical alert
data = [sick(0)] + [{}] * 10 + [sick(1)] + [normal(m) for m in range(12, 20)]   # no history -> no jump hold
clinical, sensor, results = run(Engine(profiles), "p5", data)
assert clinical == [], clinical
print("B4: bad minute + gap + bad minute -> not counted as 'held' OK")

# B5: explanations - every clinical alert shows the total, each vital's value + points, level rule, profile fact
def check_explained(a):
    r = a["rule"]
    assert r.startswith(f"NEWS2-style total {a['score']} "), r
    assert "Level rule:" in r and "Persistence:" in r and " pt" in r, r
    assert a["vital"] in ("hr", "spo2", "rr", "temp") and a["value"] is not None, a
    assert a["profile_fact"], a


for pid, fact in [("p1", "condition 'Hypercapnic respiratory failure'"),
                  ("p3", "medication 'Metoprolol succinate 50 MG'")]:
    prof = next(p for p in profiles if p["id"] == pid)
    data = list(v for ts, v in generate("slow_deterioration", 240, prof))
    clinical, sensor, results = run(Engine(profiles), pid, data)
    assert clinical, pid
    for a in clinical:
        check_explained(a)
        assert fact in a["profile_fact"], (pid, a["profile_fact"])
    first = clinical[0]                                       # slow drift is caught first by the personal baseline
    assert "Drift (triggered this alert)" in first["rule"], first["rule"]
    assert first["rule"].split("Drift (triggered this alert): ")[1].startswith(
        {"hr": "HR", "rr": "RR", "spo2": "SpO2", "temp": "Temp"}[first["vital"]]), first   # vital = drifting vital
print("B5: p1 names its Scale 2 condition, p3 names its beta-blocker, drift alerts show the drifting vital OK")

# vital/value = the vital that added the most points; no profile modifiers for p2
data = [normal(m, hr=95 + m % 2, rr=26 + m % 2) for m in range(10)]
clinical, sensor, results = run(Engine(profiles), "p2", data)
a = clinical[0]
check_explained(a)
assert a["level"] == "REVIEW" and a["vital"] == "rr" and a["value"] in (26, 27), a
assert "RR scored 3 points on its own -> REVIEW" in a["rule"], a["rule"]
assert "HR 9" in a["rule"] and "= 1 pt" in a["rule"], a["rule"]        # HR 95/96 = 1 point is listed too
assert a["profile_fact"] == "No profile modifiers (standard NEWS2 Scale 1)", a["profile_fact"]
print("B5: single 3-point vital -> REVIEW, vital = RR, 'No profile modifiers' OK")

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
