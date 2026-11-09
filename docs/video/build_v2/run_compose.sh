#!/bin/bash
# usage: PW_PY=<playwright venv python> TTS_PY=<venv python with numpy, soundfile, PIL> run_compose.sh "<cost phrase>"
# (renders the two diagram cards, then composes the video; compose_v2.py --plan prints the timeline without rendering)
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
PYTHONIOENCODING=utf-8 "${PW_PY:?set PW_PY}" render_diag.py d1 d3 || exit 1
COST_PHRASE="$1" PYTHONIOENCODING=utf-8 "${TTS_PY:?set TTS_PY}" -u compose_v2.py
