#!/usr/bin/env bash
# End-to-end regression test for autonod v0.2.
#
# For each scenario, paints a fake Claude Code prompt into a tmux pane,
# invokes `autonod decide-once`, and asserts the chosen action.
#
# Run from repo root:
#     cargo build --release && bash tests/smoke_tmux.sh
#
# Env overrides:
#     BIN=./target/debug/autonod bash tests/smoke_tmux.sh

set -uo pipefail

BIN=${BIN:-./target/release/autonod}
# Point the LLM at a dead port so rule-path "none" results stay "none"
# instead of being clobbered by a real Ollama reply.
LLM_ENDPOINT=${LLM_ENDPOINT:-http://127.0.0.1:1/never}
SESSION=autonod_smoke

if [[ ! -x "$BIN" ]]; then
  echo "FATAL: binary not found: $BIN" >&2
  echo "Build it first: cargo build --release" >&2
  exit 2
fi
if ! command -v tmux >/dev/null; then
  echo "FATAL: tmux not on PATH" >&2
  exit 2
fi
if ! command -v python3 >/dev/null; then
  echo "FATAL: python3 not on PATH" >&2
  exit 2
fi

TMPDIR_ANOD=$(mktemp -d -t autonod-smoke.XXXXXX)
cleanup() {
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  rm -rf "$TMPDIR_ANOD"
}
trap cleanup EXIT
tmux kill-session -t "$SESSION" 2>/dev/null || true
sleep 0.1

# "name|expected_action" + payload written to $TMPDIR_ANOD/<name>.txt
# (this avoids any shell quoting around apostrophes in labels).
declare -a NAMES EXPECTED
add_scenario() {
  local name=$1 expected=$2 file="$TMPDIR_ANOD/$1.txt"
  NAMES+=("$name")
  EXPECTED+=("$expected")
  # The body is the next argument; write verbatim so apostrophes etc are safe.
  printf '%s' "$3" > "$file"
}

add_scenario cursor_yes 1 "Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again
  3. No, with feedback
"
add_scenario recommended 2 "Which option?
  1. Yes
  2. Yes, allow all (recommended)
  3. No
"
add_scenario yn_inline y "Apply these changes? (y/N)
"
add_scenario two_choice_yes_no 1 "Continue with the operation?
  1. Yes
  2. No
"
add_scenario destructive none "Force push to main?
❯ 1. Yes
  2. No
"
add_scenario ambiguous none "Pick one?
  1. Yes, and don't ask again
  2. Yes
  3. No
"
add_scenario free_text none "What is your project name?
>
"
add_scenario idle none ""
add_scenario scrollback_noise 1 "running tests...
all 14 passed
Do you want to proceed?
❯ 1. Yes
  2. No
"

N=${#NAMES[@]}

# Big virtual size so all panes fit; re-tile after every split.
tmux new-session -d -s "$SESSION" -x 320 -y 200 'bash --norc --noprofile' \
  >/dev/null
for _ in $(seq 2 "$N"); do
  tmux split-window -d -t "$SESSION:0" 'bash --norc --noprofile' >/dev/null
  tmux select-layout -t "$SESSION:0" tiled >/dev/null
done
sleep 0.3

# Map our scenario index -> tmux pane index. tmux assigns pane indices in
# order of creation; with default base-index 0 the first pane is .0.
for i in $(seq 0 $((N - 1))); do
  name=${NAMES[$i]}
  target="$SESSION:0.$i"
  payload_file="$TMPDIR_ANOD/$name.txt"
  if [[ -s "$payload_file" ]]; then
    # `cat` echoes the file verbatim — no quoting hazards.
    tmux send-keys -t "$target" "clear; cat $payload_file" Enter
  else
    tmux send-keys -t "$target" "clear" Enter
  fi
done
sleep 0.8

extract_action() {
  # NOTE: `python3 - <<EOF` would consume our stdin as the script; use -c
  # to keep stdin free for the piped autonod output.
  python3 -c '
import json, sys
text = sys.stdin.read()
parts = text.split("--- decision ---")
if len(parts) < 2:
    print("PARSE_ERROR")
    sys.exit(0)
try:
    obj = json.loads(parts[1].strip())
    print(obj["action"])
except Exception:
    print("PARSE_ERROR")
'
}

pass=0
fail=0
failures=()
for i in $(seq 0 $((N - 1))); do
  name=${NAMES[$i]}
  expected=${EXPECTED[$i]}
  target="$SESSION:0.$i"

  raw=$("$BIN" decide-once "$target" --llm-endpoint "$LLM_ENDPOINT" 2>/dev/null \
        || true)
  action=$(printf '%s' "$raw" | extract_action)

  if [[ "$action" == "$expected" ]]; then
    printf "  PASS  %-22s expected=%-4s got=%s\n" "$name" "$expected" "$action"
    pass=$((pass + 1))
  else
    printf "  FAIL  %-22s expected=%-4s got=%s\n" "$name" "$expected" "$action"
    fail=$((fail + 1))
    failures+=("$name")
    echo "        ----- captured screen -----"
    tmux capture-pane -p -t "$target" -S -10 | sed 's/^/        | /'
    echo "        ----- raw decide-once output -----"
    printf '%s\n' "$raw" | sed 's/^/        | /'
  fi
done

echo
echo "scenarios: $N  pass: $pass  fail: $fail"
if (( fail > 0 )); then
  echo "FAILED: ${failures[*]}"
  exit 1
fi
echo "ALL PASS"
