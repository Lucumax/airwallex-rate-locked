"""Kokoro 82M, voice af_heart, offline. Run with a TTS venv:
  PYTHONPATH=<kokoro addon dir> HF_HOME=<HF cache dir> HF_HUB_OFFLINE=1 <your TTS venv>/python tts_kokoro.py [--keep-length] [scene ids or line ids]
Env COST_PHRASE fills VARIANT_COST in the narration (spoken and caption), e.g. "Eleven cents less this time".
Writes _work/audio/<scene>_<n>.wav and _work/clips.json (merged when scene ids are given). Each clip also gets "ends": the end time
of each sentence inside the clip (found from the quietest gaps), so the recorder and the composer can cue a screen to a sentence.
The brand name is spoken with a phoneme override (PRONOUNCE, v2.4): the default reading reduced "wall", and speech-to-text heard "Airwall X".
The override changes only the text given to the voice; the stored "tts" text and the captions keep the plain spelling.
--keep-length: a re-voiced clip keeps the length it had in _work/clips.json (trailing silence is cut, only if quiet, or padded), so the
timeline, the shots and the captions of the cut stay exactly as they were."""
import json, os, re, sys
from pathlib import Path
import numpy as np, soundfile as sf
from kokoro import KPipeline

HERE = Path(__file__).resolve().parent
OUT = HERE / "_work" / "audio"; OUT.mkdir(parents=True, exist_ok=True)
SPEED = 1.05
COST = os.environ.get("COST_PHRASE", "Eleven cents less this time")
KEEP = "--keep-length" in sys.argv
only = set(a for a in sys.argv[1:] if not a.startswith("--"))
PRONOUNCE = [(re.compile(r"\bAirwallex's"), "[Airwallex's](/ˈɛɹwˌɔlɛksᵻz/)"), (re.compile(r"\bAirwallex\b(?!')"), "[Airwallex](/ˈɛɹwˌɔlɛks/)")]


def spoken(txt):
    for rx, rep in PRONOUNCE:
        txt = rx.sub(rep, txt)
    return txt


def keep_length(a, n, thr=0.01):
    if len(a) > n:
        if np.abs(a[n:]).max() >= thr: return a  # the cut would remove speech: keep the new length
        return a[:n]
    return np.concatenate([a, np.zeros(n - len(a), dtype=np.float32)])
p = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
SR = 24000


def trim(a, thr=0.01, pad=0.05):
    idx = np.where(np.abs(a) > thr)[0]
    if len(idx) == 0: return a, 0
    i0 = max(0, idx[0] - int(pad * SR))
    return a[i0: min(len(a), idx[-1] + int(pad * 2 * SR))], i0


def sentence_count(txt):
    return len([s for s in re.split(r"(?<=[.!?])\s+", txt.strip()) if s])


def sentence_ends(a, n):
    """end time of each of the n sentences: the n-1 longest quiet gaps (>= 0.1 s) mark the boundaries, in order"""
    if n <= 1: return [len(a) / SR]
    w = int(0.01 * SR)
    env = np.array([np.abs(a[i:i + w]).max() for i in range(0, len(a) - w, w)])
    quiet = env < 0.012
    gaps, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]: j += 1
            if j - i >= 10 and i > 20 and j < len(quiet) - 5: gaps.append((j - i, (i + j) / 2 * 0.01))
            i = j
        else: i += 1
    best = sorted(gaps, reverse=True)[: n - 1]
    ends = sorted(t for _, t in best)
    if len(ends) < n - 1: ends = [len(a) / SR * (k + 1) / n for k in range(n - 1)]
    return ends + [len(a) / SR]


spec = json.loads((HERE / "narration.json").read_text(encoding="utf-8"))
cf = HERE / "_work" / "clips.json"
clips = json.loads(cf.read_text()) if (only and cf.exists()) else {}
for sc in spec["scenes"]:
    for i, ln in enumerate(sc["lines"]):
        name = "%s_%d" % (sc["id"], i)
        if only and sc["id"] not in only and name not in only: continue  # a scene id, or one line id such as u3_1
        txt = ln["tts"].replace("VARIANT_COST", COST)
        parts = [np.asarray(r.audio, dtype=np.float32) for r in p(spoken(txt), voice="af_heart", speed=SPEED)]
        a, _ = trim(np.concatenate(parts))
        if KEEP and name in clips:
            a = keep_length(a, round(clips[name]["dur"] * SR))
        sf.write(OUT / (name + ".wav"), a, SR)
        ends = sentence_ends(a, sentence_count(txt))
        clips[name] = {"tts": txt, "dur": len(a) / SR, "voice": "kokoro:af_heart", "ends": [round(float(e), 2) for e in ends]}
        print(name, round(len(a) / SR, 2), len(txt.split()), clips[name]["ends"], flush=True)
cf.write_text(json.dumps(clips, indent=1))
print("total speech", round(sum(c["dur"] for c in clips.values()), 1))
