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


UNITS = {"hr": " bpm", "spo2": "%", "rr": "/min", "temp": " C"}
NAMES = {"hr": "HR", "spo2": "SpO2", "rr": "RR", "temp": "Temp"}
PLAUSIBLE = {"hr": (20, 250), "spo2": (50, 100), "rr": (3, 60), "temp": (30, 43)}
BETA_BLOCKERS = ["metoprolol", "atenolol", "bisoprolol", "carvedilol", "propranolol", "nebivolol"]

# Sudden-jump check. ENGINEERING CHOICES, NOT CLINICAL FACTS - tune them on real data (e.g. BIDMC).
# A reading this far from the median of the last 5 good readings is held back until confirmed.
JUMP_LIMIT = {"hr": 30, "rr": 8, "spo2": 6, "temp": 1.0}
JUMP_CONFIRM = 2          # how many more readings must stay near the new level before we believe it...
JUMP_CONFIRM_SECONDS = 60  # ...AND for at least this long (so 1 Hz data needs a minute, not 2 seconds)
# Motion-noise check (also an ENGINEERING CHOICE): if the raw readings of the last 60 s (but never fewer
# than the last 5 readings) scatter more than this (standard deviation), the wearer is probably moving.
# Simulator (1 reading/min): 5 readings = 5 min. BIDMC 1 Hz: 60 readings. Measured: a 5-min window at 1 Hz
# found HR noise only after ~110 s and RR noise never; 60 s finds both in under a minute.
MOTION_WINDOW = timedelta(seconds=60)
MOTION_MIN_READINGS = 5
MOTION_LIMIT = {"hr": 15, "rr": 5}
# ...and it only counts as "calm again" below this lower level, so the check does not flicker on/off.
MOTION_CALM = {"hr": 9, "rr": 3}
# Flatline (ENGINEERING CHOICE): heart rate EXACTLY the same for at least this many readings AND this long.
# Both are needed: BIDMC 1 Hz data can repeat one integer HR for many seconds (many readings, little time),
# and the simulator sends 1 reading/minute (lots of time, few readings).
FLATLINE_READINGS = 10
# A vital's sensor-problem episode only ends after this long without any artifact, so a burst of bad
# readings (e.g. 1 Hz motion) gives ONE SENSOR alert, not dozens. ENGINEERING CHOICE.
SENSOR_EPISODE_GAP = timedelta(minutes=5)
FLATLINE_MINUTES = 5


def spread(entries):
    """Sample standard deviation of the values in [(time, value), ...]. 0 if fewer than 2 values."""
    vals = [x for _, x in entries]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5


def parse_ts(ts):
    """ISO time text -> datetime. Accepts a trailing 'Z' (UTC) too."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class Engine:
    def __init__(self, profiles, persist_minutes=2, calm_minutes=20, baseline_size=30, gap_minutes=5):
        # persist_minutes = a level must hold this long (by reading timestamps) before we alert
        # calm_minutes    = after this long fully calm, the patient can be alerted again from WATCH
        # gap_minutes     = no reading for longer than this -> SENSOR alert "no data received"
        # ENGINEERING CHOICES (from the team plan), not clinical facts.
        self.gap = timedelta(minutes=gap_minutes)
        self.last_seen = {}                       # pid -> time of the newest reading received (any vital)
        self.gap_alerted = {}                     # pid -> True if the current gap already has its SENSOR alert
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
        self.last_artifact = defaultdict(dict)                            # pid -> {vital: time of last bad reading}
        self.pending = defaultdict(lambda: defaultdict(list))             # pid -> {vital: [held jump readings]}
        self.jump_start = defaultdict(dict)                               # pid -> {vital: time the jump began}
        self.window = defaultdict(lambda: defaultdict(deque))            # last 5 min of (time, raw value)
        self.in_motion = defaultdict(dict)                                # pid -> {vital: True while noisy}
        self.same_hr = {}                                                 # pid -> [value, first time, count]
        self.probe = defaultdict(dict)            # pid -> {reason key: text}; not empty = probe likely removed

    def profile(self, pid):
        return self.profiles.get(pid, {"id": pid, "conditions": [], "medications": [], "spo2_scale": 1})

    # ---------------- probe removal: SpO2 0/None and/or heart-rate flatline ----------------
    def update_probe(self, pid, vital, value, now):
        """Keeps the list of reasons why the finger probe looks removed. Returns True if a NEW episode starts."""
        reasons = self.probe[pid]
        was_off = bool(reasons)
        if vital == "spo2":
            if value is None or value <= 0:
                reasons["spo2"] = "no SpO2 value" if value is None else "SpO2 = 0"
            else:
                reasons.pop("spo2", None)
        if vital == "hr" and value is not None:
            same = self.same_hr.get(pid)
            if same and same[0] == value:
                same[2] += 1
            else:
                same = self.same_hr[pid] = [value, now, 1]              # value changed -> start counting again
            minutes = (now - same[1]).total_seconds() / 60
            if same[2] >= FLATLINE_READINGS and minutes >= FLATLINE_MINUTES:
                reasons["hr"] = f"heart rate flat at {value:g} for {minutes:.0f} min ({same[2]} readings)"
            else:
                reasons.pop("hr", None)
        return bool(reasons) and not was_off

    # ---------------- sensor artifact detection ----------------
    def check_artifact(self, pid, vital, value, now):
        if vital in ("spo2", "hr") and self.probe[pid]:          # same finger sensor gives SpO2 and HR
            return "probe likely removed (" + "; ".join(self.probe[pid].values()) + f") -> {vital} not trusted"
        if value is None:
            return "no value from sensor (dropout)"
        lo, hi = PLAUSIBLE[vital]
        if not lo <= value <= hi:
            return f"physiologically implausible {vital} = {value}"
        return self.check_jump_and_motion(pid, vital, value, now)

    def check_jump_and_motion(self, pid, vital, value, now):
        """Motion noise first if we are already in a noisy spell, then sudden jumps, then new motion noise."""
        if vital not in MOTION_LIMIT:
            return self.check_jump(pid, vital, value, now)
        w = self.window[pid][vital]
        w.append((now, value))
        while len(w) > MOTION_MIN_READINGS and now - w[0][0] >= MOTION_WINDOW:   # last 60 s, >= 5 readings
            w.popleft()
        if self.in_motion[pid].get(vital):
            if spread(w) > MOTION_CALM[vital]:
                return self.motion_text(vital, w)
            self.in_motion[pid][vital] = False                 # calm again -> start fresh
            self.pending[pid][vital].clear()
        jump = self.check_jump(pid, vital, value, now)         # (resets the window when a jump is confirmed)
        if jump:
            return jump
        w = self.window[pid][vital]
        if len(w) >= MOTION_MIN_READINGS and spread(w) > MOTION_LIMIT[vital]:
            self.in_motion[pid][vital] = True
            self.pending[pid][vital].clear()
            return self.motion_text(vital, w)
        return None

    @staticmethod
    def motion_text(vital, w):
        return (f"motion noise: last {len(w)} {vital} readings too unsteady "
                f"(std {spread(w):.1f} > {MOTION_LIMIT[vital]}), waiting for it to calm down")

    def check_jump(self, pid, vital, value, now):
        """Hold back a sudden jump until it is confirmed: JUMP_CONFIRM more readings near the new level
        AND at least JUMP_CONFIRM_SECONDS since the jump began. Returns artifact text or None."""
        limit = JUMP_LIMIT[vital]
        held = self.pending[pid][vital]
        if held:
            if abs(value - held[0]) <= limit / 2:              # still at the new level
                held.append(value)
                waited = (now - self.jump_start[pid][vital]).total_seconds()
                if len(held) > JUMP_CONFIRM and waited >= JUMP_CONFIRM_SECONDS:   # confirmed -> real change
                    self.recent[pid][vital].extend(held[:-1])  # (process() adds the current reading itself)
                    start = self.jump_start[pid][vital]            # a clean step is not motion noise:
                    self.window[pid][vital] = deque(e for e in self.window[pid][vital] if e[0] >= start)
                    held.clear()
                    return None
                return (f"sudden jump in {vital} to {value:g}, awaiting confirmation "
                        f"({len(held) - 1}/{JUMP_CONFIRM} readings, {waited:.0f}/{JUMP_CONFIRM_SECONDS} s)")
            held.clear()                                       # it went away -> it was a spike, forget it
        good = list(self.recent[pid][vital])[-5:]
        if len(good) >= 5 and abs(value - median(good)) > limit:
            held.append(value)
            self.jump_start[pid][vital] = now
            return (f"sudden jump in {vital} from usual {median(good):g} to {value:g}, awaiting confirmation "
                    f"(needs {JUMP_CONFIRM} more readings and {JUMP_CONFIRM_SECONDS} s)")
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
            u = UNITS[vital]
            why = " (lowered for beta-blocker)" if vital == "hr" and prof.get("on_beta_blocker") else ""
            return (f"{NAMES[vital]} average of last 5 readings {now:.1f}{u} vs personal baseline {b:.1f}{u} "
                    f"(change {d:+.1f}{u}, drift limit {lim:+g}{u}{why})")
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

    # ---------------- missing data (device or connection problem) ----------------
    def gap_alert(self, pid, ts, minutes):
        return dict(patient_id=pid, ts=ts, level="SENSOR", score=None, vital=None, value=None,
                    rule=f"No data received for {minutes} minutes (device or connection problem) "
                         f"- not a clinical alert", profile_fact="-")

    def check_missing(self, now_ts):
        """Call every ~30 s (Person A, from the API). Returns ONE SENSOR alert per gap, for every patient
        whose newest reading is older than gap_minutes. Patients never seen yet are not checked."""
        now = parse_ts(now_ts)
        alerts = []
        for pid, seen in self.last_seen.items():
            if now - seen > self.gap and not self.gap_alerted.get(pid):
                self.gap_alerted[pid] = True                     # don't repeat for this same gap
                alerts.append(self.gap_alert(pid, now_ts, int((now - seen).total_seconds() // 60)))
        return alerts

    def note_reading_time(self, pid, now, ts):
        """Called for every reading. Returns a SENSOR alert list if this reading ends a long gap."""
        alerts = []
        seen = self.last_seen.get(pid)
        if seen is not None and now - seen > self.gap:
            if not self.gap_alerted.get(pid):                   # check_missing may have alerted already
                alerts.append(self.gap_alert(pid, ts, int((now - seen).total_seconds() // 60)))
            self.since[pid].clear()                             # we can't know the level during the gap,
                                                                # so "held continuously" starts again
        if seen is None or now > seen:
            self.last_seen[pid] = now
            self.gap_alerted[pid] = False
        return alerts

    # ---------------- explanations (what the clinician reads) ----------------
    def explain(self, level, alert_level, held, total, parts, drifts, drift_caused, L, scale):
        """Rule text: NEWS2 total, every vital's value and points, the level rule, persistence, drift."""
        shown = []
        for k in ("hr", "spo2", "rr", "temp"):
            if k in parts:
                extra = f" (Scale {scale})" if k == "spo2" else ""
                shown.append(f"{NAMES[k]} {L[k]:g}{UNITS[k]}{extra} = {parts[k]} pt")
        text = f"NEWS2-style total {total} from wearable vitals [{', '.join(shown)}]"
        missing = [NAMES[k] for k in ("hr", "spo2", "rr", "temp") if k not in parts]
        if missing:
            text += f". Not scored (no trusted reading): {', '.join(missing)}"
        threes = [NAMES[k] for k, p in parts.items() if p == 3]
        if total >= 7:
            lvl = f"total {total} >= 7 -> URGENT"
        elif total >= 5:
            lvl = f"total {total} >= 5 -> REVIEW"
        elif threes:
            lvl = f"{threes[0]} scored 3 points on its own -> REVIEW"
        elif total >= 3:
            lvl = f"total {total} >= 3 -> WATCH"
        else:
            lvl = f"total {total} < 3 but drift from personal baseline -> WATCH (early warning)"
        text += f". Level rule: {lvl}"
        mins = self.persist.total_seconds() / 60
        text += (f". Persistence: {alert_level} or higher held {held.total_seconds() / 60:.1f} min "
                 f"(rule: >= {mins:g} min)")
        if alert_level != level:
            text += f"; {level} has not yet held {mins:g} min"
        if drifts:
            label = "Drift (triggered this alert)" if drift_caused else "Drift also seen"
            text += f". {label}: " + "; ".join(d for _, d in drifts)
        return text

    @staticmethod
    def profile_fact(prof, parts):
        """Name the exact profile facts that changed the scoring, or say there were none."""
        facts = []
        if prof.get("spo2_scale") == 2 and "spo2" in parts:
            cond = next((c for c in prof.get("conditions", []) if "hypercapnic" in c.lower()), None)
            source = f"condition '{cond}'" if cond else "spo2_scale = 2"
            facts.append(f"SpO2 scored on NEWS2 Scale 2 (target 88-92%) because profile lists {source}")
        if prof.get("on_beta_blocker"):
            meds = prof.get("medications", [])
            med = next((m for m in meds if any(b in m.lower() for b in BETA_BLOCKERS)), None)
            source = f"medication '{med}'" if med else "on_beta_blocker = true"
            facts.append(f"Beta-blocker ({source}): heart-rate rise may be blunted, so HR drift limit is "
                         f"lowered from 15 to 10 bpm")
        return "; ".join(facts) or "No profile modifiers (standard NEWS2 Scale 1)"

    # ---------------- main entry ----------------
    def process(self, pid, vital, value, ts):
        prof = self.profile(pid)
        now = parse_ts(ts)
        alerts = self.note_reading_time(pid, now, ts)

        new_probe = self.update_probe(pid, vital, value, now)
        art = self.check_artifact(pid, vital, value, now)
        if art:
            if vital in ("spo2", "hr") and self.probe[pid]:     # probe episode: ONE alert for the whole patient
                if new_probe:
                    alerts.append(dict(patient_id=pid, ts=ts, level="SENSOR", score=None, vital=vital, value=value,
                                       rule=f"Sensor check: {art}. SpO2 and heart rate excluded and clinical "
                                            f"alerts paused until the probe is back.", profile_fact="-"))
            elif now - self.last_artifact[pid].get(vital, now - SENSOR_EPISODE_GAP) >= SENSOR_EPISODE_GAP:
                alerts.append(dict(patient_id=pid, ts=ts, level="SENSOR", score=None, vital=vital, value=value,
                                   rule=f"Sensor check: {art}. Reading excluded from clinical scoring.",
                                   profile_fact="-"))       # one SENSOR alert per episode, not every reading
            self.last_artifact[pid][vital] = now
            self.latest[pid].pop(vital, None)                   # its old value is not trusted any more either
            self.latest_ts[pid].pop(vital, None)
            return {"score": None, "level": None, "artifact": art, "alerts": alerts}

        self.recent[pid][vital].append(value)
        if len(self.baseline[pid][vital]) < self.baseline_size:
            self.baseline[pid][vital].append(value)
        self.latest[pid][vital] = value
        self.latest_ts[pid][vital] = now

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

        # only vitals we trust right now (in L) can drift; a vital in a sensor episode is left out
        drifts = [(v, d) for v in ("hr", "rr", "spo2", "temp") if v in L and (d := self.drift(pid, v, prof))]
        drift_caused = bool(drifts) and level == "NONE"
        if drift_caused:
            level = "WATCH"                                       # rule 2: slow drift -> early Watch

        # persistence (by time): alert only if a level has held for persist_minutes, and only when it escalates
        causes = [k for k, p in parts.items() if p > 0] + [v for v, _ in drifts]
        evidence = min((self.latest_ts[pid][k] for k in causes), default=now)
        alert_level, held = self.persistence(pid, level, now, evidence)
        if LEVELS.index(alert_level) > LEVELS.index(self.alerted_level[pid]) and not self.probe[pid]:
            self.alerted_level[pid] = alert_level
            if drift_caused:
                worst = drifts[0][0]                              # alert caused by drift -> show that vital
            else:
                worst = max(parts, key=parts.get)                 # the vital that added the most points
            rule = self.explain(level, alert_level, held, total, parts, drifts, drift_caused, L, scale)
            alerts.append(dict(patient_id=pid, ts=ts, level=alert_level, score=total, vital=worst,
                               value=L.get(worst), rule=rule, profile_fact=self.profile_fact(prof, parts)))

        return {"score": total, "level": level, "artifact": None, "alerts": alerts}
