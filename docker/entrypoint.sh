#!/usr/bin/env bash
# Headless server and explicit one-shot CLI commands. Interactive login runs locally.
set -euo pipefail
cmd="${1:-server}"
if [ "$#" -gt 0 ]; then shift; fi
if [ "$cmd" = "--help" ] || [ "$cmd" = "help" ]; then
  echo "configure init|doctor | server [--status|--once] | responses [--dry-run] | chats | resumes list|select|fetch | session check|export | raise | watch"
  echo "Configuration: --config /data/config.server.toml. Local login: python scripts/session.py login."
  exit 0
fi
if [ "$cmd" = "configure" ]; then
  exec python /app/scripts/configure.py "$@"
fi
case "$cmd" in
  server|responses|chats|resumes|session|raise|watch) ;;
  login|loop) echo "Use local scripts/session.py login; recurring jobs use the server command." >&2; exit 2 ;;
  *) exec "$cmd" "$@" ;;
esac
DATA="${HH_AUTO_HOME:-/data}"
CONFIG="${HH_SERVER_CONFIG:-$DATA/config.server.toml}"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) [ "$#" -ge 2 ] || { echo "--config requires a path" >&2; exit 2; }; CONFIG="$2"; shift 2 ;;
    --config=*) CONFIG="${1#--config=}"; shift ;;
    *) args+=("$1"); shift ;;
  esac
done
if [ ! -f "$CONFIG" ]; then
  echo "Missing $CONFIG. Copy config.server.example.toml into the data directory and configure it first." >&2
  exit 2
fi
case "$cmd" in
  server) exec python /app/scripts/server.py --config "$CONFIG" "${args[@]}" ;;
  responses) exec python /app/scripts/responses.py --config "$CONFIG" "${args[@]}" ;;
  chats) exec python /app/scripts/chats.py --config "$CONFIG" "${args[@]}" ;;
  resumes) exec python /app/scripts/resumes.py --config "$CONFIG" "${args[@]}" ;;
  session)
    if [ "${args[0]:-}" = "login" ]; then
      echo "Interactive login runs locally: python scripts/session.py login --config hh-data/config.server.toml" >&2
      exit 2
    fi
    exec python /app/scripts/session.py --config "$CONFIG" "${args[@]}" ;;
  raise) exec python /app/scripts/raise_resume.py --config "$CONFIG" "${args[@]}" ;;
  watch) exec python /app/scripts/watch.py --config "$CONFIG" "${args[@]}" ;;
esac
