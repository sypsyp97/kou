#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
JAR="${TLA_JAR:-$HOME/.local/share/tla/tla2tools.jar}"
JAVA="${TLA_JAVA:-$HOME/.local/share/temurin-jre-21/Contents/Home/bin/java}"
if [ ! -x "$JAVA" ]; then JAVA="$(command -v java || true)"; fi
if [ -z "$JAVA" ] || [ ! -r "$JAR" ]; then
  echo 'TLC requires Java and tla2tools.jar; set TLA_JAVA and TLA_JAR.' >&2
  exit 1
fi
"$JAVA" -version >/dev/null 2>&1 || { echo 'Java runtime is unavailable.' >&2; exit 1; }
OUT="${TLA_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/kou-tlc.XXXXXX")}"
mkdir -p "$OUT"
printf 'Logs: %s\n' "$OUT"
failed=0
ran=0
while read -r name expected; do
  [ -n "$name" ] || continue
  if [ "$#" -gt 0 ]; then
    selected=0
    for wanted in "$@"; do [ "$wanted" != "$name" ] || selected=1; done
    [ "$selected" = 1 ] || continue
  fi
  module="${name%%-*}"
  ran=$((ran + 1))
  log="$OUT/$name.log"
  code=0
  "$JAVA" -Xmx768m -XX:ActiveProcessorCount=2 -XX:+UseParallelGC -cp "$JAR" tlc2.TLC \
    -workers 2 -cleanup -coverage 1 -teSpecOutDir "$OUT" -metadir "$OUT/$name.states" \
    -config "$ROOT/$name.cfg" "$ROOT/$module.tla" >"$log" 2>&1 || code=$?
  if [ "$expected" = pass ]; then
    if [ "$code" = 0 ] && grep -q 'Model checking completed. No error has been found.' "$log"; then
      # An action that never fires makes the passing result vacuous for that step.
      dead="$(grep -E '^<[A-Z][A-Za-z0-9]* line .*>: [0-9]+:0$' "$log" | sed -E 's/^<([A-Za-z0-9]+) .*/\1/' | tr '\n' ' ' || true)"
      if [ -n "$dead" ]; then
        printf 'FAIL %s: unreachable actions: %s\n' "$name" "$dead"; failed=1; continue
      fi
      printf 'PASS %s: ' "$name"
    else
      printf 'FAIL %s (exit %s): %s\n' "$name" "$code" "$log"; failed=1; tail -20 "$log"; continue
    fi
  elif [ "$code" != 0 ] && grep -q "Invariant $expected is violated" "$log"; then
    if [[ "$name" = Clipboard-platform-* ]]; then
      printf 'PLATFORM COUNTEREXAMPLE %s (%s): ' "$name" "$expected"
    else
      printf 'COUNTEREXAMPLE %s (%s): ' "$name" "$expected"
    fi
  else
    printf 'FAIL %s: expected %s counterexample: %s\n' "$name" "$expected" "$log"
    failed=1; tail -20 "$log"; continue
  fi
  grep 'states generated,' "$log" || true
done <<'CASES'
Hotkey pass
Hotkey-edge DebouncedEdge
Hotkey-debounce DebouncedEdge
Capture pass
Capture-generation UniqueGenerations
Capture-recorder NoStaleEffects
Capture-transcript NoStaleEffects
Capture-recovery NoStaleEffects
Capture-retry RetryAtMostOnce
Capture-lifecycle NoStaleEffects
Clipboard pass
Clipboard-paste-session GuardedOperations
Clipboard-paste-focus GuardedOperations
Clipboard-paste-board GuardedOperations
Clipboard-restore-board GuardedOperations
Clipboard-late-count NoAdoptedOwnership
Clipboard-marker pass
Clipboard-broadcast DirectedDelivery
Clipboard-platform-claim StrictClaim
Clipboard-platform-restore StrictRestore
Clipboard-platform-focus StrictFocusedDelivery
Clipboard-platform-content StrictClipboardAtDelivery
Shutdown pass
Shutdown-launch NoWorkAfterQuit
Shutdown-drain ReplyAfterCleanup
Shutdown-sweep ReplyAfterCleanup
Shutdown-once ReplyOnce
Identity pass
Identity-bundle StableIdentity
Identity-requirement StableIdentity
Runtime pass
Runtime-probe ReadyIsCurrent
Runtime-exit ReadyIsCurrent
Runtime-backoff NoSpuriousBackoff
AsrServer pass
AsrServer-reuse NoCrossedReply
AsrServer-lock NoStolenSocket
CASES
[ "$ran" -gt 0 ] || { echo 'No matching configuration.' >&2; exit 1; }
exit "$failed"
