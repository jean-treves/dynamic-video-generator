#!/usr/bin/env bash
# Render the repository's demo clips with the real pipeline, and write the
# sidecar each one needs to appear in the dashboard with measured numbers.
#
# Everything under docs/demo/ has to be output of THIS pipeline. A clip from a
# hosted service illustrates that service, not this one — and a reader can tell,
# because the resolutions, the frame counts and the seconds per step do not
# match what a 16 GB machine produces.
#
# Usage:  ./scripts/make_demo.sh            # needs the render backend reachable
#         PROXY=http://192.168.1.20:8200 ./scripts/make_demo.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/docs/demo"
PROXY="${PROXY:-http://127.0.0.1:8200}"
mkdir -p "$OUT"

# name|prompt|width|height|frames|quality
# 704x416 x 121f = 35 M pixel-frames: inside the 16 GB budget, ~12 s/step.
CLIPS=(
"harbour-dawn|A harbour quay at dawn, mist lifting off the water as a crane rises slowly over moored boats. Cold blue light warms toward the horizon. Gulls call over the low hum of an idling engine.|704|416|121|standard"
"neon-rain|A rain-slicked alley under magenta and cyan neon, static locked-off shot, water running down a grated drain. Reflections ripple as a sign flickers. Rain patters on metal and a distant siren fades.|704|416|97|standard"
"workshop-dust|Dust motes drift through a shaft of light in a cluttered workshop, the camera tilting slowly up past hanging tools. Warm overcast light. A clock ticks under the creak of settling timber.|576|320|121|quick"
"coast-fog|A coastal road swallowed by fog, locked-off shot, the guardrail vanishing into white. Muted grey light, no horizon. Wind buffets the microphone and waves break far below.|704|416|97|standard"
)

command -v ffprobe >/dev/null || { echo "ffprobe not found (brew install ffmpeg)" >&2; exit 1; }
curl -fsS "$PROXY/health" >/dev/null || { echo "proxy unreachable at $PROXY" >&2; exit 1; }

for row in "${CLIPS[@]}"; do
  IFS='|' read -r name prompt w h frames quality <<<"$row"
  echo "── $name  ${w}x${h} ${frames}f ($quality)"
  start=$(date +%s)
  id=$(curl -fsS -X POST "$PROXY/queue/add" -H 'Content-Type: application/json' \
       -d "$(python3 - "$prompt" "$w" "$h" "$frames" "$quality" <<'PY'
import json, sys
p, w, h, f, q = sys.argv[1:6]
print(json.dumps({"mode": "t2v", "prompt": p, "width": int(w), "height": int(h),
                  "frames": int(f), "quality": q}))
PY
)" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')
  [ -n "$id" ] || { echo "  queue/add returned no id" >&2; exit 1; }

  # Poll until the job leaves the queue, then take the newest output.
  while curl -fsS "$PROXY/status" | grep -q "$id"; do sleep 5; done
  elapsed=$(( $(date +%s) - start ))
  src=$(curl -fsS "$PROXY/outputs?limit=1" | python3 -c 'import json,sys; o=json.load(sys.stdin)["outputs"]; print(o[0]["path"] if o else "")')
  [ -n "$src" ] || { echo "  no output produced" >&2; exit 1; }

  curl -fsS "$PROXY/file?path=$src" -o "$OUT/$name.mp4"
  case "$quality" in
    quick) steps=8 ;;
    high)  steps=14 ;;
    *)     steps=10 ;;
  esac
  python3 - "$OUT/$name.mp4" "$OUT/$name.json" "$prompt" "$steps" "$elapsed" <<'PY'
import json, subprocess, sys
clip, out, prompt, steps, elapsed = sys.argv[1:6]
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0",
     "-show_entries", "stream=width,height,nb_frames,r_frame_rate",
     "-of", "json", clip], capture_output=True, text=True, check=True)
st = json.loads(probe.stdout)["streams"][0]
num, den = (st["r_frame_rate"].split("/") + ["1"])[:2]
json.dump({
    "prompt": prompt,
    "width": st["width"], "height": st["height"],
    "frames": int(st.get("nb_frames") or 0),
    "fps": round(int(num) / int(den)),
    "steps": int(steps),
    "render_seconds": int(elapsed),
}, open(out, "w"), indent=2)
PY
  echo "  -> $name.mp4 (${elapsed}s wall clock)"
done

# One still per clip, for the Images tab.
for row in "${CLIPS[@]}"; do
  name="${row%%|*}"
  ffmpeg -v error -y -ss 1.0 -i "$OUT/$name.mp4" -frames:v 1 -q:v 3 "$OUT/$name-still.jpg"
  python3 - "$OUT/$name.json" "$OUT/$name-still.json" <<'PY'
import json, sys
src, dst = sys.argv[1:3]
p = json.load(open(src))
json.dump({"prompt": p["prompt"], "width": p["width"], "height": p["height"],
           "frames": 1, "fps": p["fps"], "steps": p["steps"],
           "render_seconds": 0}, open(dst, "w"), indent=2)
PY
done

echo
echo "Done. $(ls "$OUT"/*.mp4 2>/dev/null | wc -l | tr -d ' ') clips in docs/demo/."
echo "Replay mode now serves these instead of the drawn placeholders."
