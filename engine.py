"""PERSON B OWNS THIS FILE.
The scoring brain. Pure Python - no web, no database - so it can be tested alone (python -m engine.test_engine).

CONTRACT (don't change without telling the team):
    engine = Engine(profiles)                       # profiles = list loaded from patients/profiles.json
    result = engine.process(patient_id, vital, value, ts_iso)
    result = {"score": int|None, "level": "NONE|WATCH|REVIEW|URGENT"|None,
              "artifact": str|None, "alerts": [alert, ...]}
    alert  = {"patient_id", "ts", "level" (WATCH|REVIEW|URGENT|SENSOR), "score",
              "vital", "value", "rule", "profile_fact"}
"""
from collections import defaultdict, deque
from statistics import median
from shared.config import LEVELS

# ---------------- NEWS2 points per vital (RCP NEWS2 chart) ----------------
# Not available from a wearable: blood pressure, consciousness, air/oxygen -> we assume room air.

def score_rr(v):
    if v <= 8: return 3
    if v <= 11: return 1
    if v <= 20: return 0
    if v <= 24: return 2
    return 3


def score_spo2(v, scale):
    if scale == 2:      # Scale 2: hypercapnic respiratory failure, target 88-92% (room air -> >=88 scores 0)
        if v <= 83: return 3
        if v <= 85: return 2
        if v <= 87: return 1
        return 0
    if v <= 91: return 3   # Scale 1 (normal patients)
    if v <= 93: return 2
    if v <= 95: return 1
    return 0


def score_temp(v):
    if v <= 35.0: return 3
    if v <= 36.0: return 1
    if v <= 38.0: return 0
    if v <= 39.0: return 1
    return 2


def score_hr(v):
    if v <= 40: return 3
    if v <= 50: return 1
    if v <= 90: return 0
    if v <= 110: return 1
    if v <= 130: return 2
    return 3


PLAUSIBLE = {"hr": (20, 250), "spo2": (50, 100), "rr": (3, 60), "temp": (30, 43)}
BETA_BLOCKERS = ["metoprolol", "atenolol", "bisoprolol", "carvedilol", "propranolol", "nebivolol"]

# Sudden-jump check. ENGINEERING CHOICES, NOT CLINICAL FACTS - tune them on real data (e.g. BIDMC).
# A reading this far from the median of the last 5 good readings is held back until confirmed.
JUMP_LIMIT = {"hr": 30, "rr": 8, "spo2": 6, "temp": 1.0}
JUMP_CONFIRM = 2          # how many more readings must stay near the new level before we believe it


class Engine:
    def __init__(self, profiles, persistence=8, baseline_size=30):
        # persistence = how many abnormal evaluations in a row before alerting
        # (each simulated minute sends 4 readings, so 8 = about 2 minutes)
        self.profiles = {p["id"]: p for p in profiles}
        self.persistence = persistence
        self.baseline_size = baseline_size
        self.latest = defaultdict(dict)                                   # pid -> {vital: last good value}
        self.recent = defaultdict(lambda: defaultdict(lambda: deque(maxlen=10)))
        self.baseline = defaultdict(lambda: defaultdict(list))            # first N good readings = personal normal
        self.streak = defaultdict(int)
        self.calm = defaultdict(int)
        self.alerted_level = defaultdict(lambda: "NONE")
        self.in_artifact = defaultdict(dict)                              # pid -> {vital: artifact text or None}
        self.pending = defaultdict(lambda: defaultdict(list))             # pid -> {vital: [held jump readings]}

    def profile(self, pid):
        return self.profiles.get(pid, {"id": pid, "conditions": [], "medications": [], "spo2_scale": 1})

    # ---------------- sensor artifact detection (TODO Person B: improve) ----------------
    def check_artifact(self, pid, vital, value):
        if value is None:
            return "no value from sensor (dropout)"
        if vital == "spo2" and value <= 0:
            return "SpO2 = 0 -> probe likely removed"
        lo, hi = PLAUSIBLE[vital]
        if not lo <= value <= hi:
            return f"physiologically implausible {vital} = {value}"
        r = self.recent[pid][vital]
        if vital == "hr" and len(r) >= 10 and all(x == value for x in r):
            return "heart rate flatline (10 identical readings) -> sensor stuck"
        spo2_problem = self.in_artifact[pid].get("spo2")
        if vital == "hr" and spo2_problem and not spo2_problem.startswith("sudden jump"):
            return "SpO2 probe off -> heart rate from same sensor not trusted"
        return self.check_jump(pid, vital, value)

    def check_jump(self, pid, vital, value):
        """Hold back a sudden jump until the next readings confirm it. Returns artifact text or None."""
        limit = JUMP_LIMIT[vital]
        held = self.pending[pid][vital]
        if held:
            if abs(value - held[0]) <= limit / 2:              # still at the new level
                held.append(value)
                if len(held) > JUMP_CONFIRM:                   # confirmed -> it is a real change
                    self.recent[pid][vital].extend(held[:-1])  # (process() adds the current reading itself)
                    held.clear()
                    return None
                return f"sudden jump in {vital} to {value:g}, awaiting confirmation ({len(held) - 1}/{JUMP_CONFIRM})"
            held.clear()                                       # it went away -> it was a spike, forget it
        good = list(self.recent[pid][vital])[-5:]
        if len(good) >= 5 and abs(value - median(good)) > limit:
            held.append(value)
            return (f"sudden jump in {vital} from usual {median(good):g} to {value:g}, "
                    f"awaiting confirmation (0/{JUMP_CONFIRM})")
        return None

    # ---------------- personalised rolling baseline ----------------
    def drift(self, pid, vital, prof):
        base = self.baseline[pid][vital]
        recent = list(self.recent[pid][vital])[-5:]
        if len(base) < self.baseline_size or len(recent) < 5:
            return None
        b, now = sum(base) / len(base), sum(recent) / len(recent)
        hr_limit = 10 if prof.get("on_beta_blocker") else 15     # rule 3: beta blockers hide HR rise
        limits = {"hr": hr_limit, "rr": 4, "spo2": -3, "temp": 0.8}
        d = now - b
        lim = limits[vital]
        if (lim > 0 and d >= lim) or (lim < 0 and d <= lim):
            return f"{vital} drifted from personal baseline {b:.1f} to {now:.1f} ({d:+.1f})"
        return None

    # ---------------- main entry ----------------
    def process(self, pid, vital, value, ts):
        prof = self.profile(pid)
        alerts = []

        art = self.check_artifact(pid, vital, value)
        if art:
            if not self.in_artifact[pid].get(vital):            # one SENSOR alert per episode, not every reading
                alerts.append(dict(patient_id=pid, ts=ts, level="SENSOR", score=None, vital=vital, value=value,
                                   rule=f"Sensor check: {art}. Reading excluded from clinical scoring.",
                                   profile_fact="-"))
            self.in_artifact[pid][vital] = art
            return {"score": None, "level": None, "artifact": art, "alerts": alerts}
        self.in_artifact[pid][vital] = None

        self.recent[pid][vital].append(value)
        if len(self.baseline[pid][vital]) < self.baseline_size:
            self.baseline[pid][vital].append(value)
        self.latest[pid][vital] = value

        # NEWS2-style score from the latest good value of each vital
        scale = prof.get("spo2_scale", 1)
        L = self.latest[pid]
        parts = {}
        if "rr" in L: parts["rr"] = score_rr(L["rr"])
        if "spo2" in L: parts["spo2"] = score_spo2(L["spo2"], scale)
        if "temp" in L: parts["temp"] = score_temp(L["temp"])
        if "hr" in L: parts["hr"] = score_hr(L["hr"])
        total = sum(parts.values())

        if total >= 7:
            level = "URGENT"
        elif total >= 5 or 3 in parts.values():
            level = "REVIEW"
        elif total >= 3:
            level = "WATCH"
        else:
            level = "NONE"

        drifts = [(v, d) for v in ("hr", "rr", "spo2", "temp") if (d := self.drift(pid, v, prof))]
        if drifts and level == "NONE":
            level = "WATCH"                                       # rule 2: slow drift -> early Watch

        # persistence: only alert if the problem holds for several readings, and only when it escalates
        if level != "NONE":
            self.streak[pid] += 1
            self.calm[pid] = 0
        else:
            self.streak[pid] = 0
            self.calm[pid] += 1
            if self.calm[pid] >= 20:
                self.alerted_level[pid] = "NONE"

        if (level != "NONE" and self.streak[pid] >= self.persistence
                and LEVELS.index(level) > LEVELS.index(self.alerted_level[pid])):
            self.alerted_level[pid] = level
            worst = max(parts, key=parts.get) if parts else vital
            if drifts and parts.get(worst, 0) == 0:
                worst = drifts[0][0]                              # alert caused by drift -> show that vital
            pts = ", ".join(f"{k.upper()} {L[k]:g}->{p}" for k, p in parts.items())
            rule = f"NEWS2 total {total} ({pts}) -> {level}"
            if drifts:
                rule += ". Rolling baseline: " + "; ".join(d for _, d in drifts)
            facts = []
            if scale == 2:
                facts.append("Hypercapnic respiratory failure -> SpO2 Scale 2 (target 88-92%)")
            if prof.get("on_beta_blocker"):
                facts.append("On beta-blocker -> HR rise may be masked, lower HR drift limit (10 bpm)")
            profile_fact = "; ".join(facts) or "No profile modifiers (standard NEWS2 Scale 1)"
            alerts.append(dict(patient_id=pid, ts=ts, level=level, score=total, vital=worst,
                               value=L.get(worst), rule=rule, profile_fact=profile_fact))

        return {"score": total, "level": level, "artifact": None, "alerts": alerts}
