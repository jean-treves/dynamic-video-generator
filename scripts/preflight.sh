#!/usr/bin/env bash
#
# Dynamic Video Generator — render preflight (run ON THE RENDER MACHINE before a big LTX render).
#
# Frees as much unified memory as possible to avoid swapping: on 16 GB, the
# moment it swaps the cost per step is ~9x (35 M pixel-frames ~ 12 s/step,
# 221 M ~ 104 s/step).
#
#   ./preflight.sh                     gentle preflight: unload Ollama, quit the
#                                      browsers, purge caches (if sudo needs no
#                                      password), GO/NO-GO verdict
#   ./preflight.sh --keep-browsers     do not quit Safari/Chrome/Arc/Firefox/Edge
#   ./preflight.sh --quit-drawthings   ALSO quit Draw Things (the 🎨 Images tab
#                                      is down until it restarts — worth it for
#                                      long renders)
#   ./preflight.sh --restart-backend   kill the :8198 backend then restart it
#                                      (PHOS_SERVER_CMD) or wait for Pinokio Start
#   ./preflight.sh --sudo              allow the sudo prompt for `purge`
#   ./preflight.sh --dry-run           show what would be done, change nothing
#
# Settings (overridable through the environment):
PHOSPHENE_PORT="${PHOSPHENE_PORT:-8198}"
PROXY_PORT="${PROXY_PORT:-8200}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
# Automatic backend restart (manual route B only — leave empty with Pinokio):
#   export PHOS_SERVER_CMD='cd ~/phosphene && .venv/bin/python -m phosphene.server --port 8198 --low-ram'
PHOS_SERVER_CMD="${PHOS_SERVER_CMD:-}"
BACKEND_LOG="/tmp/phos_backend_restart.log"

# GO/NO-GO thresholds (GB of free RAM) — render peak ~14 GB per docs/INSTALL_M4.md,
# but macOS recycles "inactive/purgeable": >= 9 free is comfortable for 704x416x97f.
GO_GB=9
WARN_GB=6

c_ok()   { printf "\033[32m✓\033[0m %s\n" "$1"; }
c_warn() { printf "\033[33m⚠\033[0m %s\n" "$1"; }
c_err()  { printf "\033[31m✗\033[0m %s\n" "$1"; }
c_info() { printf "\033[36m·\033[0m %s\n" "$1"; }

KEEP_BROWSERS=0; QUIT_DT=0; RESTART_BACKEND=0; USE_SUDO=0; DRY=0
for a in "$@"; do
  case "$a" in
    --keep-browsers)   KEEP_BROWSERS=1 ;;
    --quit-drawthings) QUIT_DT=1 ;;
    --restart-backend) RESTART_BACKEND=1 ;;
    --sudo)            USE_SUDO=1 ;;
    --dry-run)         DRY=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) c_warn "option inconnue : $a (voir --help)" ;;
  esac
done
run() { # run "description" cmd…
  local desc="$1"; shift
  if [ "$DRY" = 1 ]; then c_info "[dry-run] $desc"; else "$@"; fi
}

# ── free RAM = free + inactive + purgeable + speculative (16 KB pages on Apple Silicon)
ram_avail_gb() {
  vm_stat | awk '
    /page size of/           { ps=$8 }
    /Pages free/             { free=$3 }
    /Pages inactive/         { inact=$3 }
    /Pages purgeable/        { purg=$3 }
    /Pages speculative/      { spec=$3 }
    END { gsub(/\./,"",free); gsub(/\./,"",inact); gsub(/\./,"",purg); gsub(/\./,"",spec);
          if (ps=="") ps=16384;
          printf "%.1f", (free+inact+purg+spec)*ps/1073741824 }'
}
swap_used() { sysctl -n vm.swapusage | awk '{print $6}'; }

echo "──────────────────────────────────────────────"
echo " Dynamic Video Generator — render preflight (16 GB)"
echo "──────────────────────────────────────────────"
BEFORE_GB=$(ram_avail_gb)
c_info "free RAM before: ${BEFORE_GB} GB · swap used: $(swap_used)"
c_info "Top consommateurs :"
ps axo rss=,comm= | sort -rn | head -5 | awk '{rss=$1; $1=""; n=split($0,p,"/"); printf "    %5.1f GB  %s\n", rss/1048576, p[n]}'

# 1) Unload the Ollama models (gemma3:4b frees ~3-4 GB; the server stays up and
#    the LLM buttons reload the model on demand — first call slower, by design)
if curl -s --max-time 3 "http://127.0.0.1:$OLLAMA_PORT/api/ps" >/tmp/phos_ollama_ps.json 2>/dev/null; then
  LOADED=$(python3 -c 'import json;print(" ".join(m["name"] for m in json.load(open("/tmp/phos_ollama_ps.json")).get("models",[])))' 2>/dev/null)
  if [ -n "$LOADED" ]; then
    for m in $LOADED; do
      run "ollama stop $m" ollama stop "$m" 2>/dev/null \
        || run "unload $m via API" curl -s --max-time 10 "http://127.0.0.1:$OLLAMA_PORT/api/generate" \
             -d "{\"model\":\"$m\",\"keep_alive\":0}" -o /dev/null
      c_ok "Ollama: $m unloaded from RAM"
    done
  else
    c_ok "Ollama: no model loaded in RAM"
  fi
else
  c_info "Ollama unreachable on :$OLLAMA_PORT — nothing to unload"
fi

# 2) Quit the browsers (gentle: AppleScript quit, tabs are restored on relaunch)
if [ "$KEEP_BROWSERS" = 1 ]; then
  c_info "browsers kept (--keep-browsers)"
else
  for app in "Safari" "Google Chrome" "Arc" "Firefox" "Microsoft Edge"; do
    if pgrep -xq "$app" 2>/dev/null || osascript -e "application \"$app\" is running" 2>/dev/null | grep -q true; then
      run "quitter $app" osascript -e "tell application \"$app\" to quit" >/dev/null 2>&1
      c_ok "$app quit (tabs restored next time it opens)"
    fi
  done
fi

# 3) Draw Things (opt-in: ~2-4 GB when an image model is loaded)
if [ "$QUIT_DT" = 1 ]; then
  if osascript -e 'application "Draw Things" is running' 2>/dev/null | grep -q true; then
    run "quitter Draw Things" osascript -e 'tell application "Draw Things" to quit' >/dev/null 2>&1
    c_ok "Draw Things quit — the 🎨 Images tab is down until it restarts"
  else
    c_info "Draw Things is not running"
  fi
else
  c_info "Draw Things kept (use --quit-drawthings for long renders)"
fi

# 4) Purge disk caches (makes "inactive" memory immediately reusable)
if [ "$DRY" = 1 ]; then
  c_info "[dry-run] purge"
elif sudo -n purge 2>/dev/null; then
  c_ok "purge done (disk caches freed)"
elif [ "$USE_SUDO" = 1 ]; then
  sudo purge && c_ok "purge done" || c_warn "purge refused"
else
  c_info "purge skipped (sudo needs a password — rerun with --sudo if you want it)"
fi

# 5) Phosphene backend (:8198) — opt-in restart to clear MLX fragmentation
if [ "$RESTART_BACKEND" = 1 ]; then
  PID=$(lsof -ti "tcp:$PHOSPHENE_PORT" 2>/dev/null | head -1)
  if [ -n "$PID" ]; then
    run "stopping backend (pid $PID)" kill "$PID" 2>/dev/null
    for i in $(seq 1 10); do lsof -ti "tcp:$PHOSPHENE_PORT" >/dev/null 2>&1 || break; sleep 1; done
    lsof -ti "tcp:$PHOSPHENE_PORT" >/dev/null 2>&1 && run "kill -9 backend" kill -9 "$PID" 2>/dev/null
    c_ok "backend :$PHOSPHENE_PORT stopped"
  else
    c_info "No backend on :$PHOSPHENE_PORT"
  fi
  if [ "$DRY" = 1 ]; then
    c_info "[dry-run] relance backend"
  elif [ -n "$PHOS_SERVER_CMD" ]; then
    c_info "Relance : $PHOS_SERVER_CMD"
    nohup bash -c "$PHOS_SERVER_CMD" >"$BACKEND_LOG" 2>&1 &
    for i in $(seq 1 60); do
      curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PHOSPHENE_PORT/status" && break; sleep 2
    done
    if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PHOSPHENE_PORT/status"; then
      c_ok "backend restarted and /status answers (log: $BACKEND_LOG)"
    else
      c_err "backend unreachable after restart — see $BACKEND_LOG"
    fi
  else
    c_warn "PHOS_SERVER_CMD not set (Pinokio backend) -> click Start in Pinokio; waiting…"
    for i in $(seq 1 60); do
      curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PHOSPHENE_PORT/status" && break; sleep 2
    done
    curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PHOSPHENE_PORT/status" \
      && c_ok "Backend back up on :$PHOSPHENE_PORT" \
      || c_err "still nothing on :$PHOSPHENE_PORT after 2 min — restart Pinokio then rerun this script"
  fi
else
  curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PHOSPHENE_PORT/status" \
    && c_ok "Phosphene backend OK on :$PHOSPHENE_PORT (not restarted — --restart-backend to clear its memory)" \
    || c_warn "No backend on :$PHOSPHENE_PORT — start the Phosphene panel (Pinokio)"
fi

# 6) Verdict
sleep 2
AFTER_GB=$(ram_avail_gb)
FREED=$(python3 -c "print(f'{max(0.0, $AFTER_GB - $BEFORE_GB):.1f}')" 2>/dev/null || echo "?")
echo "──────────────────────────────────────────────"
c_info "free RAM after: ${AFTER_GB} GB (freed ~${FREED} GB) · swap: $(swap_used)"
VERDICT=$(python3 -c "
a=$AFTER_GB
print('GO' if a>=$GO_GB else 'WARN' if a>=$WARN_GB else 'NOGO')" 2>/dev/null || echo WARN)
case "$VERDICT" in
  GO)   c_ok  "GO — start your render (704x416x97f is comfortable, stay <= 40 M pixel-frames)" ;;
  WARN) c_warn "TIGHT — 704x416x97f in quick/balanced only; rerun with --quit-drawthings and/or --restart-backend" ;;
  NOGO) c_err "NO-GO — less than ${WARN_GB} GB free: reboot the Mac or close what is left (see top above)" ;;
esac
echo "──────────────────────────────────────────────"
