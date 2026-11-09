#!/bin/bash
# usage: start_console.sh <port> <clone_dir> <log>
# Starts the SANDBOX console (live model reader) from a clean clone. Keys come from the private wrapper (never printed).
# The sandbox sometimes drops a call at start-up; retry up to 6 times. Prints READY when the port answers.
PORT=$1; CLONE=$2; LOG=$3
KEYS=${KEYS_WRAPPER:?set KEYS_WRAPPER to a script that loads the sandbox keys}
cd "$CLONE" || exit 1
for i in 1 2 3 4 5 6; do
  RATE_LOCKED_BENEFICIARIES=${BENS:?set BENS to your private beneficiaries file} PYTHONPATH=src PYTHONUNBUFFERED=1 \
    nohup "$KEYS" python -m rate_locked serve --port $PORT --world sandbox --reader model > "$LOG" 2>&1 &
  SP=$!
  for s in $(seq 1 240); do
    sleep 0.5
    if curl -s -o /dev/null http://127.0.0.1:$PORT/static/app.css; then echo READY attempt $i; exit 0; fi
    kill -0 $SP 2>/dev/null || break
  done
  echo "attempt $i did not come up"; kill $SP 2>/dev/null
done
exit 1
