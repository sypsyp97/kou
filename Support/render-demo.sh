#!/bin/bash
# Records the notch overlay off screen and writes docs/media/island-{light,dark}.gif.
# Requires ffmpeg and Screen Recording permission for the terminal.
# Frame timing depends on system load; the script keeps the take with the fewest late frames.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
xcrun swiftc -O -o "$WORK/island-demo" "$ROOT/Sources/Kou/Island.swift" "$ROOT/Support/IslandDemo/main.swift"
late_frames() {
  python3 -c 'import sys; t=[float(x) for x in open(sys.argv[1]).read().split()]; print(sum(b-a>0.06 for a,b in zip(t[3:],t[4:])))' "$1/times.txt"
}
for theme in light dark; do
  best=""; best_late=999
  for take in 1 2 3 4 5 6; do
    "$WORK/island-demo" "$WORK/$theme-$take" "$theme"
    late="$(late_frames "$WORK/$theme-$take")"
    if [ "$late" -lt "$best_late" ]; then best="$WORK/$theme-$take"; best_late="$late"; fi
    [ "$late" -le 2 ] && break
  done
  echo "$theme: $best_late late frames"
  python3 - "$best" > "$WORK/$theme.concat" <<'PY'
import sys, pathlib
d = pathlib.Path(sys.argv[1]); t = [float(x) for x in (d / "times.txt").read_text().split()]
for i, x in enumerate(t):
    print(f"file '{d}/{i:04d}.png'\nduration {(t[i + 1] - x) if i + 1 < len(t) else 0.6:.4f}")
print(f"file '{d}/{len(t) - 1:04d}.png'")
PY
  ffmpeg -loglevel error -y -f concat -safe 0 -i "$WORK/$theme.concat" \
    -vf "fps=50,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a" \
    -loop 0 "$ROOT/docs/media/island-$theme.gif"
done
