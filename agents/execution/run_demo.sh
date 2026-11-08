#!/usr/bin/env bash
# One command for the stage demo (agents/data/out/demo_script.md), repeatable:
#
#   agents/execution/run_demo.sh            # live Gemini
#   agents/execution/run_demo.sh --replay   # recorded agents (REPLAY banner), everything else live
#
# Starts a clean case 1 stack (alibi.demo --reset, ALIBI_MODE=agents), checks it, opens the
# holder and Bank A windows, then walks the Driver beats one Enter at a time:
#   D1  the £2,160 payout lands in Maya's bank feed and Alibi raises its alert
#   D2  both clocks move past real time, then Bank A sends its Request for Context
#   D3  Bank B sends its request
# After D3, Enter resets everything (a fresh --reset start) and runs the demo again; q quits.
#
# Options: --replay, --port N (app port; merchant N+1, bank N+2; default 8100),
#          --root DIR (demo data dir; default data/alibi/demo), --auto (no prompts, no
#          windows: runs D1-D3 once, then keeps the stack up until Ctrl-C; for rehearsal checks),
#          --no-open (print the window URLs but do not open them).
# Tokens are read from data/alibi/bank_tokens.json and never printed: the Bank A URL is opened
# directly and the Bank B URL is put on the clipboard for a private window.
set -euo pipefail

repo="${ALIBI_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$repo"

replay=""
port=8100
root="data/alibi/demo"
auto=0
no_open=0
while [ $# -gt 0 ]; do
  case "$1" in
    --replay) replay="--replay" ;;
    --port) port="$2"; shift ;;
    --root) root="$2"; shift ;;
    --auto) auto=1 ;;
    --no-open) no_open=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

app="http://127.0.0.1:$port"
merchant="http://127.0.0.1:$((port + 1))"
bank="http://127.0.0.1:$((port + 2))"
urls=(--app-url "$app" --merchant-url "$merchant" --bank-url "$bank")
log="data/alibi/run_demo_$port.log"
tokens="data/alibi/bank_tokens.json"
stack_pid=""

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*"; exit 1; }

drive() { uv run --frozen python -m alibi.demo "$@" "${urls[@]}"; }

# The stack runs as a background job, and bash starts background jobs with SIGINT ignored, so
# stop it (uv and the Python under it) with SIGTERM, then make sure its ports are free again.
tree() { local p; for p in $(pgrep -P "$1" 2>/dev/null); do tree "$p"; done; echo "$1"; }
stop_stack() {
  if [ -n "$stack_pid" ]; then
    local pids; pids="$(tree "$stack_pid")"
    kill -TERM $pids 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 $pids 2>/dev/null || break; sleep 0.25; done
    kill -9 $pids 2>/dev/null || true
  fi
  stack_pid=""
  for _ in $(seq 1 40); do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.25
  done
}
trap 'stop_stack; echo; echo "Stopped."; exit 0' INT TERM

token_for() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$tokens" "$1"
}

start_stack() {
  for p in "$port" "$((port + 1))" "$((port + 2))"; do
    if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
      fail "Port $p is already in use (another demo running?). Stop it, or use --port $((port + 10))."
    fi
  done
  mkdir -p data/alibi
  say "Starting a clean case 1 stack${replay:+ in REPLAY mode} on $app ..."
  ALIBI_MODE=agents uv run --frozen python -m alibi.demo --timeout 14400 --case 1 --reset \
    --app-port "$port" --merchant-port "$((port + 1))" --bank-port "$((port + 2))" \
    --root "$root" $replay >"$log" 2>&1 &
  stack_pid=$!
  for _ in $(seq 1 240); do
    if grep -q '"passport_seed"' "$log" 2>/dev/null; then break; fi
    kill -0 "$stack_pid" 2>/dev/null || { tail -20 "$log"; fail "The stack exited. Log: $log"; }
    sleep 0.5
  done
  grep -q '"passport_seed": "connectors"' "$log" || { tail -20 "$log"; fail "Stack did not come up seeded from connectors. Log: $log"; }
  grep -q '"merchant_pushed": 19' "$log" || echo "  warning: merchant_pushed is not 19 (see $log)"
  local status
  status="$(drive status)"
  echo "$status" | grep -q '"mode": "agents"' || fail "App is not in agents mode: $status"
  echo "  ready: connectors seeded, agents mode${replay:+, replay}. Log: $log"
}

open_windows() {
  [ "$auto" = 1 ] && return 0
  local a b
  a="$(token_for bank_a)"; b="$(token_for bank_b)"
  [ -n "$a" ] && [ -n "$b" ] || fail "Bank tokens missing in $tokens"
  say "Windows (separate cookies: holder on localhost, banks on 127.0.0.1):"
  echo "  Holder (phone width): http://localhost:$port/passport"
  if [ "$no_open" = 0 ] && command -v open >/dev/null && command -v pbcopy >/dev/null; then
    echo "  Bank A:               opened in your browser (token in the URL, not printed)"
    echo "  Bank B:               URL copied to the clipboard; paste it into a PRIVATE window"
    open "http://localhost:$port/passport"
    open "$app/bank/bank_a/inbox?token=$a"
    printf '%s' "$app/bank/bank_b/inbox?token=$b" | pbcopy
  else
    echo "  Bank A:               $app/bank/bank_a/inbox?token=<bank_a in $tokens>"
    echo "  Bank B:               $app/bank/bank_b/inbox?token=<bank_b in $tokens> (private window)"
  fi
  echo "  Also bookmark http://localhost:$port/sources/rec-0001-020/1 (404 until D1)."
}

pause() {
  [ "$auto" = 1 ] && return 0
  printf '\n\033[36m%s\033[0m  [Enter] ' "$1"
  read -r _
}

expect_accepted() {
  echo "$1"
  echo "$1" | grep -q '"status": "accepted"' || fail "The request was not accepted by the app (see above)."
}

later() { date -u -v+15M +%Y-%m-%dT%H:%M:00Z 2>/dev/null || date -u -d '+15 min' +%Y-%m-%dT%H:%M:00Z; }

run_once() {
  start_stack
  open_windows
  pause "0:00  Holder shows Maya, no alerts, no requests. Next: D1 (payout lands, alert raised)."
  say "D1  advance to 2026-10-28T07:30:00Z"
  drive advance --to 2026-10-28T07:30:00Z
  echo "  Holder: reload /passport, open the Unexplained credit alert, then rec-0001-020."
  pause "Next: D2 (clock past real time, then Bank A's request)."
  local t; t="$(later)"
  say "D2  advance to $t, then rfc bank_a"
  drive advance --to "$t"
  expect_accepted "$(drive rfc bank_a)"
  echo "  Holder: reload, open the Bank A request, Run investigation, Approve for Bank A, Send."
  echo "  Bank A: reload the inbox, open the package, record a disposition (Clear)."
  pause "Next: D3 (Bank B's request)."
  say "D3  rfc bank_b"
  expect_accepted "$(drive rfc bank_b)"
  echo "  Holder: open the Bank B request, Run, Approve for Bank B, Send. Reload Bank B's inbox."
}

while true; do
  run_once
  if [ "$auto" = 1 ]; then
    say "D1-D3 done. The stack keeps running until Ctrl-C (log $log)."
    wait "$stack_pid" || true
    exit 0
  fi
  printf '\n\033[36mDemo done. Enter = reset and run again, q = stop the stack and quit.\033[0m '
  read -r answer
  stop_stack
  [ "$answer" = "q" ] && { echo "Stopped."; exit 0; }
  say "Resetting ..."
done
