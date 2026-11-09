"""LISTEN-check by transcribing the FINAL mp4's audio back (faster-whisper base.en, CPU) and comparing it to the script line by line,
using the line start times of the composed video (_work/final_timeline.json). Writes _work/asr_check.txt.
Run with the asr venv: <your ASR venv>/python asr_check.py   (FFMPEG env var points at ffmpeg)"""
import difflib, json, os, re, subprocess
from pathlib import Path
from faster_whisper import WhisperModel

H = Path(__file__).resolve().parent; W = H / "_work"; OUT = H.parent
FF = os.environ.get("FFMPEG", "ffmpeg")
wav = W / "asr_input.wav"
subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(OUT / "rate_locked_sandbox_v2_4.mp4"), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
m = WhisperModel("base.en", device="cpu", compute_type="int8", download_root=os.environ.get("WHISPER_DIR"))
segs, _ = m.transcribe(str(wav), language="en", word_timestamps=True)
words = [(w.start, w.end, w.word.strip()) for s in segs for w in s.words]
tl = json.loads((W / "final_timeline.json").read_text())
clips = json.loads((W / "clips.json").read_text())
norm = lambda s: re.sub(r"[^a-z0-9' ]", " ", s.lower().replace("-", " ")).split()
# spoken numbers are written as words in the script; the ASR may write digits
digits = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "11": "eleven", "12": "twelve", "15": "fifteen"}
def canon(ws): return [digits.get(w, w) for w in ws]
lines, bad = [], 0
prev_end = 0.0
for ln in tl["lines"]:
    said = canon(norm(clips[ln["id"]]["tts"]))
    # whisper stamps the first word after a silence at the start of the silence: open the window back to just after the previous line
    lo = max(prev_end + 0.05, ln["start"] - 1.5)
    heard_words = [w for (a, b, w) in words if a >= lo and a <= ln["start"] + ln["dur"] + 0.25]
    prev_end = ln["start"] + ln["dur"]
    heard = canon(norm(" ".join(heard_words)))
    r = difflib.SequenceMatcher(None, said, heard).ratio()
    diff = [f"{op[0]}:{' '.join(said[op[1]:op[2]])}->{' '.join(heard[op[3]:op[4]])}" for op in difflib.SequenceMatcher(None, said, heard).get_opcodes() if op[0] != "equal"]
    if r < 0.9: bad += 1
    lines.append(f"{ln['id']}  start {ln['start']:.1f}s  match {r:.2f}\n  said:  {clips[ln['id']]['tts']}\n  heard: {' '.join(heard_words)}\n  diff:  {diff}")
(W / "asr_check.txt").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines)); print("lines below 0.90 match:", bad, "of", len(tl["lines"]))
