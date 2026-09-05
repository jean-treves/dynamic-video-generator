#!/usr/bin/env bash
#
# Dynamic Video Generator — one-click launcher.
#   ./start.sh             start proxy + ngrok, check Phosphene/Ollama, open the pages
#   ./start.sh --cloudflare Cloudflare tunnel (FREE, no bandwidth cap)
#   ./start.sh --no-ngrok  local only (pages pointed at 127.0.0.1:8200)
#   ./start.sh stop        stop the proxy + ngrok + cloudflared started by this script
#
# Settings (overridable through the environment):
NGROK_DOMAIN="${NGROK_DOMAIN:-}"   # reserved ngrok domain; empty = random URL
PROXY_PORT="${PROXY_PORT:-8200}"
PHOSPHENE_PORT="${PHOSPHENE_PORT:-8198}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma3:4b}"
DT_PROXY_PORT="${DT_PROXY_PORT:-7861}"
DRAWTHINGS_PORT="${DRAWTHINGS_PORT:-7860}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"          # repository root (this script lives in scripts/)
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
PROXY_LOG="/tmp/phos_proxy.log"
DT_PROXY_LOG="/tmp/drawthings_proxy.log"
NGROK_LOG="/tmp/phos_ngrok.log"
BROWSER="${BROWSER:-Safari}"

c_ok()   { printf "\033[32m✓\033[0m %s\n" "$1"; }
c_warn() { printf "\033[33m⚠\033[0m %s\n" "$1"; }
c_err()  { printf "\033[31m✗\033[0m %s\n" "$1"; }
c_info() { printf "\033[36m·\033[0m %s\n" "$1"; }

stop_all() {
  c_info "Stopping the proxies (ports $PROXY_PORT, $DT_PROXY_PORT), ngrok and cloudflared…"
  lsof -ti "tcp:$PROXY_PORT" 2>/dev/null | xargs kill -9 2>/dev/null
  lsof -ti "tcp:$DT_PROXY_PORT" 2>/dev/null | xargs kill -9 2>/dev/null
  pkill -f "ngrok http" 2>/dev/null
  pkill -f "cloudflared tunnel" 2>/dev/null
  c_ok "Stopped. (Phosphene, Draw Things and Ollama are left running.)"
}

if [ "$1" = "stop" ]; then stop_all; exit 0; fi

# Tunnel: cloudflare (recommended, free with no bandwidth cap), ngrok (the
# default, but the free quota is limited), or none (--no-ngrok / --local).
TUNNEL="ngrok"
case "$1" in
  --cloudflare|--cf) TUNNEL="cloudflare" ;;
  --no-ngrok|--local) TUNNEL="none" ;;
esac

echo "──────────────────────────────────────────────"
echo " Dynamic Video Generator launcher"
echo "──────────────────────────────────────────────"

# 1) Phosphene
if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PHOSPHENE_PORT/"; then
  c_ok "Phosphene en marche (127.0.0.1:$PHOSPHENE_PORT)"
else
  c_err "Phosphene NOT detected on :$PHOSPHENE_PORT — start the Phosphene panel (Pinokio) first."
fi

# 2) Ollama
if curl -s --max-time 3 "http://127.0.0.1:$OLLAMA_PORT/api/tags" >/tmp/phos_tags.json 2>/dev/null; then
  if grep -q "\"$OLLAMA_MODEL\"" /tmp/phos_tags.json; then
    c_ok "Ollama running · model $OLLAMA_MODEL present"
  else
    c_warn "Ollama running but $OLLAMA_MODEL missing -> 'ollama pull $OLLAMA_MODEL' (otherwise the proxy falls back to an available model)"
  fi
else
  c_warn "Ollama NOT detected on :$OLLAMA_PORT — run 'ollama serve' (the LTX/voice/storyboard rewrite buttons depend on it)"
fi

# 2b) Draw Things (optional — image generation)
if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$DRAWTHINGS_PORT/"; then
  c_ok "Draw Things API en marche (127.0.0.1:$DRAWTHINGS_PORT)"
else
  c_warn "Draw Things NOT detected on :$DRAWTHINGS_PORT — enable the API server (Advanced tab) if you want to generate images."
fi

# 3) CORS proxy (frees its own port on start)
c_info "Starting the CORS proxy…"
nohup python3 -m dynamic_video_generator.proxy >"$PROXY_LOG" 2>&1 &
sleep 1.5
if lsof -ti "tcp:$PROXY_PORT" >/dev/null 2>&1; then
  c_ok "CORS proxy on :$PROXY_PORT  (log: $PROXY_LOG)"
else
  c_err "the proxy did not start — see $PROXY_LOG"; tail -5 "$PROXY_LOG"
fi

# 3b) Draw Things CORS proxy (frees its own port on start)
DT_PROXY_MODULE="${DT_PROXY_MODULE:-dynamic_video_generator.drawthings_proxy}"
if python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$DT_PROXY_MODULE') else 1)" 2>/dev/null; then
  c_info "Starting the Draw Things proxy…"
  nohup python3 -m "$DT_PROXY_MODULE" >"$DT_PROXY_LOG" 2>&1 &
  sleep 1.5
  if lsof -ti "tcp:$DT_PROXY_PORT" >/dev/null 2>&1; then
    c_ok "Draw Things proxy on :$DT_PROXY_PORT  (log: $DT_PROXY_LOG)"
  else
    c_err "the Draw Things proxy did not start — see $DT_PROXY_LOG"; tail -5 "$DT_PROXY_LOG"
  fi
else
  c_warn "Draw Things proxy module not found ($DT_PROXY_MODULE) — DT proxy not started."
fi

# 4) Tunnel public
ENDPOINT="http://127.0.0.1:$PROXY_PORT"
pkill -f "ngrok http" 2>/dev/null; pkill -f "cloudflared tunnel" 2>/dev/null; sleep 0.5
if [ "$TUNNEL" = "cloudflare" ]; then
  if command -v cloudflared >/dev/null 2>&1; then
    c_info "Starting the Cloudflare tunnel (free, no bandwidth cap)…"
    : > "$NGROK_LOG"
    nohup cloudflared tunnel --url "http://localhost:$PROXY_PORT" >"$NGROK_LOG" 2>&1 &
    # wait for the trycloudflare URL
    CF_URL=""
    for i in $(seq 1 30); do
      CF_URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$NGROK_LOG" | head -1)
      [ -n "$CF_URL" ] && break; sleep 1
    done
    if [ -n "$CF_URL" ]; then
      echo "$CF_URL" > /tmp/phos_tunnel_url.txt
      c_ok "Cloudflare actif : $CF_URL  (log: $NGROK_LOG)"
      ENDPOINT="$CF_URL"
      # Firebase bridge: publish the URL to the Realtime DB (the frontend reads it live)
      FB="${FIREBASE_DB:-}"
      if curl -s -X PUT -H "Content-Type: application/json" \
           -d "\"$CF_URL\"" "$FB/tunnel/url.json" >/dev/null 2>&1; then
        c_ok "URL published to Firebase ($FB/tunnel/url.json)"
        CF_NAME=$(echo "$CF_URL" | sed -E "s#https://([^.]+)\..*#\1#")
        curl -s -X PUT -H "Content-Type: application/json" -d "\"$CF_NAME\"" "$FB/tunnel/name.json" >/dev/null 2>&1
        c_info "tunnel name: $CF_NAME"
      else
        c_warn "Firebase publish failed (check the RTDB write rules)"
      fi
    else
      c_warn "Cloudflare started but the URL is not ready yet — see $NGROK_LOG"
    fi
  else
    c_err "cloudflared not found -> install it: brew install cloudflared"
  fi
elif [ "$TUNNEL" = "ngrok" ]; then
  # With no NGROK_DOMAIN, ngrok picks a random URL that is only readable from
  # its log, so ENDPOINT cannot be pre-filled.
  if [ -n "$NGROK_DOMAIN" ]; then
    c_info "Starting ngrok -> $NGROK_DOMAIN…"
    nohup ngrok http --domain="$NGROK_DOMAIN" "$PROXY_PORT" >"$NGROK_LOG" 2>&1 &
  else
    c_info "Starting ngrok (random URL — set NGROK_DOMAIN for a fixed domain)…"
    nohup ngrok http "$PROXY_PORT" >"$NGROK_LOG" 2>&1 &
  fi
  sleep 2
  if pgrep -f "ngrok http" >/dev/null; then
    if [ -n "$NGROK_DOMAIN" ]; then
      c_ok "ngrok actif : https://$NGROK_DOMAIN  (log: $NGROK_LOG)"
      ENDPOINT="https://$NGROK_DOMAIN"
    else
      c_ok "ngrok running — URL in $NGROK_LOG"
    fi
    c_warn "free ngrok = limited bandwidth. If throttled: ./start.sh --cloudflare"
  else
    c_err "ngrok did not start — see $NGROK_LOG"
  fi
else
  c_info "local mode: endpoint = http://127.0.0.1:$PROXY_PORT (no public tunnel)"
fi

# 5) Aggregated health through the proxy
sleep 0.5
if curl -s --max-time 4 "http://127.0.0.1:$PROXY_PORT/health" >/tmp/phos_health.json 2>/dev/null; then
  c_ok "proxy health: $(cat /tmp/phos_health.json)"
fi

# 6) Open the pages
c_info "Opening the interfaces in $BROWSER…"
# Pages SERVED BY THE PROXY (same origin) -> the UI works whatever the URL is,
# with no hardcoded endpoint (no more churn from a changing Cloudflare URL).
PXY="http://127.0.0.1:$PROXY_PORT"
open -a "$BROWSER" "$PXY/" "$PXY/storyboard" "$PXY/gallery" 2>/dev/null || open "$PXY/"

echo "──────────────────────────────────────────────"
c_ok "Ready. Endpoint to use in the pages: $ENDPOINT"
c_info "Draw Things (images): through $ENDPOINT/dt (same tunnel, 🎨 Images page). Optional direct proxy on :$DT_PROXY_PORT"
echo "   Stop with:  $DIR/start.sh stop"
echo "──────────────────────────────────────────────"
