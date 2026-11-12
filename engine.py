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
from datetime import datetime, timedelta
from statistics import median, stdev
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
# Motion-noise check (also an ENGINEERING CHOICE): if the last 5 raw readings scatter more than this
# (standard deviation), the wearer is probably moving -> readings are not trusted until it calms down.
MOTION_LIMIT = {"hr": 15, "rr": 5}
# ...and it only counts as "calm again" below this lower level, so the check does not flicker on/off.
MOTION_CALM = {"hr": 9, "rr": 3}


def parse_ts(ts):
    """ISO time text -> datetime. Accepts a trailing 'Z' (UTC) too."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class Engine:
    def __init__(self, profiles, persist_minutes=2, calm_minutes=20, baseline_size=30):
        # persist_minutes = a level must hold this long (by reading timestamps) before we alert
        # calm_minutes    = after this long fully calm, the patient can be alerted again from WATCH
        # ENGINEERING CHOICES (from the team plan), not clinical facts.
        self.profiles = {p["id"]: p for p in profiles}
        self.persist = timedelta(minutes=persist_minutes)
        self.calm_for = timedelta(minutes=calm_minutes)
        self.baseline_size = baseline_size
        self.latest = defaultdict(dict)                                   # pid -> {vital: last good value}
        self.latest_ts = defaultdict(dict)                                # pid -> {vital: time of that value}
        self.recent = defaultdict(lambda: defaultdict(lambda: deque(maxlen=10)))
        self.baseline = defaultdict(lambda: defaultdict(list))            # first N good readings = personal normal
        self.since = defaultdict(dict)            # pid -> {level: time the patient first reached >= that level}
        self.calm_since = {}                      # pid -> time the patient became calm (level NONE)
        self.last_ts = {}                         # pid -> time of the previous reading
        self.slot = defaultdict(timedelta)        # pid -> time between readings (what one reading "covers")
        self.alerted_level = defaultdict(lambda: "NONE")
        self.in_artifact = defaultdict(dict)                              # pid -> {vital: artifact text or None}
        self.pending = defaultdict(lambda: defaultdict(list))             # pid -> {vital: [held jump readings]}
        self.window = defaultdict(lambda: defaultdict(lambda: deque(maxlen=5)))  # last 5 raw readings (motion)
        self.in_motion = defaultdict(dict)                                # pid -> {vital: True while noisy}

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
        return self.check_jump_and_motion(pid, vital, value)

    def check_jump_and_motion(self, pid, vital, value):
        """Motion noise first if we are already in a noisy spell, then sudden jumps, then new motion noise."""
        if vital not in MOTION_LIMIT:
            return self.check_jump(pid, vital, value)
        w = self.window[pid][vital]
        w.append(value)
        if self.in_motion[pid].get(vital):
            if stdev(w) > MOTION_CALM[vital]:
                return self.motion_text(vital, w)
            self.in_motion[pid][vital] = False                 # calm again -> start fresh
            self.pending[pid][vital].clear()
        jump = self.check_jump(pid, vital, value)              # (resets the window when a jump is confirmed)
        if jump:
            return jump
        if self.noisy(vital, self.window[pid][vital]):
            self.in_motion[pid][vital] = True
            self.pending[pid][vital].clear()
            return self.motion_text(vital, self.window[pid][vital])
        return None

    @staticmethod
    def noisy(vital, w):
        return len(w) >= 5 and stdev(w) > MOTION_LIMIT[vital]

    @staticmethod
    def motion_text(vital, w):
        return (f"motion noise: last 5 {vital} readings too unsteady "
                f"(std {stdev(w):.1f} > {MOTION_LIMIT[vital]}), waiting for it to calm down")

    def check_jump(self, pid, vital, value):
        """Hold back a sudden jump until the next readings confirm it. Returns artifact text or None."""
        limit = JUMP_LIMIT[vital]
        held = self.pending[pid][vital]
        if held:
            if abs(value - held[0]) <= limit / 2:              # still at the new level
                held.append(value)
                if len(held) > JUMP_CONFIRM:                   # confirmed -> it is a real change
                    self.recent[pid][vital].extend(held[:-1])  # (process() adds the current reading itself)
                    self.window[pid][vital] = deque(held, maxlen=5)  # a clean step is not motion noise
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

    # ---------------- time-based persistence ----------------
    def persistence(self, pid, level, now, evidence):
        """Returns (highest level held for >= persist_minutes, how long it has held). Uses reading timestamps.
        evidence = when ALL the readings that cause the current level were last measured. We count held time
        up to then, plus one reading "slot" (a reading stands for the time until the next one: 1 min in the
        simulator, 1 s for BIDMC). The slot is capped at 1 min so a data gap is never counted."""
        if pid in self.last_ts and now > self.last_ts[pid]:
            self.slot[pid] = min(now - self.last_ts[pid], timedelta(minutes=1))
        self.last_ts[pid] = now
        since = self.since[pid]
        for lv in ("WATCH", "REVIEW", "URGENT"):
            if LEVELS.index(level) >= LEVELS.index(lv):
                since.setdefault(lv, now)               # remember when this level started
            else:
                since.pop(lv, None)                     # level dropped below lv -> its clock stops
        if level == "NONE":
            self.calm_since.setdefault(pid, now)
            if now - self.calm_since[pid] >= self.calm_for:
                self.alerted_level[pid] = "NONE"        # calm long enough -> start over
        else:
            self.calm_since.pop(pid, None)
        best, held = "NONE", timedelta(0)
        for lv in ("WATCH", "REVIEW", "URGENT"):
            if lv in since and evidence - since[lv] + self.slot[pid] >= self.persist:
                best, held = lv, evidence - since[lv] + self.slot[pid]
        return best, held

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
            self.latest[pid].pop(vital, None)                   # its old value is not trusted any more either
            self.latest_ts[pid].pop(vital, None)
            return {"score": None, "level": None, "artifact": art, "alerts": alerts}
        self.in_artifact[pid][vital] = None

        self.recent[pid][vital].append(value)
        if len(self.baseline[pid][vital]) < self.baseline_size:
            self.baseline[pid][vital].append(value)
        self.latest[pid][vital] = value
        self.latest_ts[pid][vital] = parse_ts(ts)

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

        # persistence (by time): alert only if a level has held for persist_minutes, and only when it escalates
        causes = [k for k, p in parts.items() if p > 0] + [v for v, _ in drifts]
        now = parse_ts(ts)
        evidence = min((self.latest_ts[pid][k] for k in causes), default=now)
        alert_level, held = self.persistence(pid, level, now, evidence)
        if LEVELS.index(alert_level) > LEVELS.index(self.alerted_level[pid]):
            self.alerted_level[pid] = alert_level
            worst = max(parts, key=parts.get) if parts else vital
            if drifts and parts.get(worst, 0) == 0:
                worst = drifts[0][0]                              # alert caused by drift -> show that vital
            pts = ", ".join(f"{k.upper()} {L[k]:g}->{p}" for k, p in parts.items())
            rule = f"NEWS2 total {total} ({pts}) -> {level}"
            rule += (f". {alert_level} or higher held for {held.total_seconds() / 60:.1f} min "
                     f"(persistence rule: >= {self.persist.total_seconds() / 60:g} min)")
            if drifts:
                rule += ". Rolling baseline: " + "; ".join(d for _, d in drifts)
            facts = []
            if scale == 2:
                facts.append("Hypercapnic respiratory failure -> SpO2 Scale 2 (target 88-92%)")
            if prof.get("on_beta_blocker"):
                facts.append("On beta-blocker -> HR rise may be masked, lower HR drift limit (10 bpm)")
            profile_fact = "; ".join(facts) or "No profile modifiers (standard NEWS2 Scale 1)"
            alerts.append(dict(patient_id=pid, ts=ts, level=alert_level, score=total, vital=worst,
                               value=L.get(worst), rule=rule, profile_fact=profile_fact))

        return {"score": total, "level": level, "artifact": None, "alerts": alerts}
