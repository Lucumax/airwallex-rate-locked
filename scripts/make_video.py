#!/usr/bin/env python3
"""Assemble a SILENT captioned walkthrough video from the frames that scripts/qa_walkthrough.mjs --video saved.

    node scripts/qa_walkthrough.mjs --stage --video docs/demo/frames --out docs/demo/steps
    python scripts/make_video.py docs/demo/frames docs/demo/rate-locked-walkthrough-FAKE-silent.mp4

Every frame is a real capture of the running console (or the approver page on a phone viewport). The only things
added are the caption bar and a fixed WORLD: FAKE strip, so the video cannot be mistaken for a sandbox run. This is
a slideshow of captures with captions, not a screen recording with a pointer. No audio. Needs ffmpeg on PATH.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

W, H, BAR = 1920, 1080, 150
FONT_SOURCES = ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
BANNER = "WORLD: FAKE (local fake of the Airwallex endpoints)   |   Test money   |   Reader: scripted TEST DOUBLE, no model"


def wrap(text: str, width: int) -> str:
    lines, cur = [], ""
    for word in text.split():
        if len(cur) + len(word) + 1 > width and cur:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    lines.append(cur)
    return "\n".join(lines)


def main(frames_dir: str, out: str) -> int:
    ff = shutil.which("ffmpeg")
    if not ff:
        print("ffmpeg not found on PATH")
        return 2
    d = Path(frames_dir)
    frames = json.loads((d / "frames.json").read_text(encoding="utf-8"))
    work = d / "_clips"
    work.mkdir(exist_ok=True)
    font = next((f for f in FONT_SOURCES if Path(f).is_file()), None)
    if font is None:
        print("no usable TrueType font found; edit FONT_SOURCES")
        return 2
    shutil.copy(font, work / "font.ttf")  # copied next to the clips so the filter needs no drive-letter path
    FONT = "_clips/font.ttf"
    (work / "banner.txt").write_bytes(BANNER.encode("utf-8"))
    clips = []
    for i, f in enumerate(frames, 1):
        cap = work / f"cap{i:02d}.txt"
        cap.write_bytes(wrap(f["caption"], 92).encode("utf-8"))  # bytes: no CRLF, drawtext would draw a blank line
        clip = f"clip{i:02d}.mp4"
        view_h = H - BAR - 44
        if f["kind"] == "phone":   # centre the phone-sized capture on a plain background
            vf = (f"[0:v]scale=-2:{view_h}[img];color=c=0xF0EFFF:s={W}x{view_h}:d={f['secs']}[bg];[bg][img]overlay=(W-w)/2:0:shortest=1[v];"
                  f"[v]pad={W}:{H}:0:44:0xFFFFFF")
        else:
            vf = f"scale={W}:{view_h}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:44:0xFFFFFF"
        vf += (f",drawbox=x=0:y=0:w={W}:h=44:color=0xE8590C@1:t=fill"
               f",drawtext=fontfile={FONT}:textfile=_clips/banner.txt:fontcolor=white:fontsize=24:x=(w-text_w)/2:y=9"
               f",drawbox=x=0:y={H-BAR}:w={W}:h={BAR}:color=0x1A1D21@1:t=fill"
               f",drawtext=fontfile={FONT}:textfile=_clips/{cap.name}:fontcolor=white:fontsize=34:x=48:y={H-BAR+24}:line_spacing=8")
        cmd = [ff, "-y", "-loglevel", "error", "-loop", "1", "-t", str(f["secs"]), "-i", f["file"],
               "-filter_complex" if f["kind"] == "phone" else "-vf", vf, "-r", "30", "-pix_fmt", "yuv420p", f"_clips/{clip}"]
        subprocess.run(cmd, cwd=d, check=True)
        clips.append(clip)
    (work / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "_clips/list.txt", "-c", "copy",
                    str(Path(out).resolve())], cwd=d, check=True)
    total = sum(f["secs"] for f in frames)
    print(f"wrote {out}: {len(frames)} frames, {total // 60}:{total % 60:02d}, silent")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
