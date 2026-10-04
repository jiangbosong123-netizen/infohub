#!/bin/zsh
# InfoHub single-host deployment on macOS (launchd): one web role, one worker role, maintenance
# commands in between. It mirrors compose.yaml: migrate first, web never crawls, the worker is the
# only scheduler and the only process that receives model credentials.
#
#   infohub-macos.sh check                       prerequisites and safety checks (no changes)
#   infohub-macos.sh install-code [SHA]          release worktree at a CI-green main SHA + venv
#   infohub-macos.sh configure LEGACY_ENV        render config; copy allowlisted worker secrets
#   infohub-macos.sh import-legacy LEGACY_DB     one-time consistent copy of an old database
#   infohub-macos.sh migrate                     stop roles, back up and migrate (prepare-release)
#   infohub-macos.sh backfill                    resumable history backfill (web may run meanwhile)
#   infohub-macos.sh verify                      validate release, worker config and database (no loops)
#   infohub-macos.sh start | stop | status       launchd web + worker
#
# Environment: INFOHUB_PREFIX (default ~/infohub-production), INFOHUB_PORT (default 8000),
# INFOHUB_REPO (default the repository containing this script), DRY_RUN=1 renders launchd files
# without loading them, ALLOW_WITH_WINDOWS=1 overrides the second-collector guard.
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
REPO="${INFOHUB_REPO:-${SCRIPT_DIR:h:h}}"
PREFIX="${INFOHUB_PREFIX:-$HOME/infohub-production}"
PORT="${INFOHUB_PORT:-8000}"
LABEL_PREFIX="${INFOHUB_LABEL_PREFIX:-com.infohub.production}"
DOMAIN="gui/$(id -u)"
WORKER_KEYS=(LLM_BASE_URL LLM_MODEL LLM_API_KEY SEC_USER_AGENT CRAWL_TICK_MINUTES
             RECONCILE_HOUR RECONCILE_MINUTE REPORT_HOUR REPORT_MINUTE AI_TICK_MINUTES)

say() { print -r -- "[infohub] $*"; }
die() { print -r -- "[infohub] ERROR: $*" >&2; exit 1; }
python312() { command -v python3.12 || ls "$HOME"/.local/bin/python3.12 2>/dev/null || true; }

check() {
  local problems=0
  [[ -n "$(python312)" ]] || { say "missing python3.12"; problems=1; }
  command -v git >/dev/null || { say "missing git"; problems=1; }
  command -v sqlite3 >/dev/null || { say "missing sqlite3"; problems=1; }
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "$DOMAIN/$LABEL_PREFIX.web" >/dev/null 2>&1; then
    say "port $PORT is already in use by another process"; problems=1
  fi
  if [[ -e "$PREFIX/code/.env" ]]; then
    say "$PREFIX/code/.env exists; the web role would load it. Remove it."; problems=1
  fi
  if launchctl print "$DOMAIN/com.infohub.server" >/dev/null 2>&1; then
    say "the legacy com.infohub.server agent is loaded; stop it first"; problems=1
  fi
  local tailscale=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  if [[ -x "$tailscale" ]] && "$tailscale" status 2>/dev/null | grep -E "windows-server" | grep -vq offline; then
    if [[ "${ALLOW_WITH_WINDOWS:-0}" != 1 ]]; then
      say "windows-server is online: two production collectors must not run at once (SPEC)"; problems=1
    fi
  fi
  (( problems == 0 )) && say "check passed (prefix $PREFIX, port $PORT)"
  return $problems
}

install_code() {
  local sha="${1:-}"
  git -C "$REPO" fetch -q origin
  [[ -n "$sha" ]] || sha=$(git -C "$REPO" rev-parse origin/main)
  git -C "$REPO" merge-base --is-ancestor "$sha" origin/main || die "$sha is not on origin/main"
  if command -v gh >/dev/null; then
    local states
    states=$(cd "$REPO" && gh api "repos/{owner}/{repo}/commits/$sha/check-runs" \
      --jq '[.check_runs[] | .conclusion] | unique | join(",")' 2>/dev/null || true)
    [[ "$states" == success ]] || die "CI for $sha is not uniformly successful (${states:-unknown})"
  else
    say "gh not found: verify CI for $sha manually before starting"
  fi
  mkdir -p "$PREFIX"
  if [[ -d "$PREFIX/code" ]]; then
    git -C "$PREFIX/code" checkout -q --detach "$sha"
  else
    git -C "$REPO" worktree add -q --detach "$PREFIX/code" "$sha"
  fi
  [[ -z "$(git -C "$PREFIX/code" status --porcelain --untracked-files=no)" ]] || die "release worktree is dirty"
  [[ -x "$PREFIX/code/.venv/bin/python" ]] || "$(python312)" -m venv "$PREFIX/code/.venv"
  "$PREFIX/code/.venv/bin/pip" install -q -r "$PREFIX/code/requirements.txt"
  print -r -- "$sha" > "$PREFIX/RELEASE"
  say "code at $sha"
}

configure() {
  local legacy_env="${1:?configure needs the old .env path for worker secrets}"
  [[ -f "$PREFIX/RELEASE" ]] || die "run install-code first"
  mkdir -p "$PREFIX/config" "$PREFIX/bin" "$PREFIX/logs" "$PREFIX/data"
  chmod 700 "$PREFIX" "$PREFIX/config" "$PREFIX/data"
  local origin="${INFOHUB_PUBLIC_ORIGIN:-}"
  if [[ -z "$origin" ]]; then
    # Read-only: the private HTTPS origin is this Mac's MagicDNS name (see PRIVATE_HTTPS_INGRESS.md).
    origin=$(/Applications/Tailscale.app/Contents/MacOS/Tailscale status --json 2>/dev/null \
      | "$PREFIX/code/.venv/bin/python" -c 'import json,sys; print("https://" + json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' \
      2>/dev/null || true)
  fi
  [[ "$origin" == https://*.ts.net ]] || die "set INFOHUB_PUBLIC_ORIGIN=https://<machine>.<tailnet>.ts.net"
  sed -e "s#@PREFIX@#$PREFIX#g" -e "s#@PORT@#$PORT#g" -e "s#@APP_VERSION@#$(<"$PREFIX/RELEASE")#g" \
    -e "s#@PUBLIC_ORIGIN@#$origin#g" "$SCRIPT_DIR/common.env.template" > "$PREFIX/config/common.env"
  : > "$PREFIX/config/worker.env"
  chmod 600 "$PREFIX/config/worker.env"
  # Parse with dotenv semantics (as the app does) and re-quote for the shell that sources it:
  # unquoted values may contain spaces, e.g. SEC_USER_AGENT="name email".
  "$PREFIX/code/.venv/bin/python" - "$legacy_env" "$PREFIX/config/worker.env" $WORKER_KEYS <<'PY'
import shlex, sys
from dotenv import dotenv_values
values = dotenv_values(sys.argv[1])
with open(sys.argv[2], "w", encoding="utf-8") as out:
    for key in sys.argv[3:]:
        if values.get(key):
            out.write(f"{key}={shlex.quote(values[key])}\n")
PY
  install -m 755 "$SCRIPT_DIR/run-role" "$PREFIX/bin/run-role"
  say "configured: origin $origin; $(grep -c . "$PREFIX/config/worker.env") worker keys copied (values not shown)"
}

import_legacy() {
  local legacy_db="${1:?import-legacy needs the old app.db path}"
  [[ ! -e "$PREFIX/data/app.db" ]] || die "$PREFIX/data/app.db already exists; import is one-time"
  mkdir -p "$PREFIX/data"
  "$PREFIX/code/.venv/bin/python" - "$legacy_db" "$PREFIX/data/app.db" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print("imported items:", target.execute("SELECT COUNT(*) FROM items").fetchone()[0])
target.close(); source.close()
PY
  chmod 600 "$PREFIX/data/app.db"
}

agent_file() { print -r -- "$HOME/Library/LaunchAgents/$LABEL_PREFIX.$1.plist"; }

stop() {
  local role
  for role in worker web; do
    if [[ "${DRY_RUN:-0}" == 1 ]]; then say "dry run: would boot out $LABEL_PREFIX.$role"; continue; fi
    launchctl bootout "$DOMAIN/$LABEL_PREFIX.$role" 2>/dev/null && say "stopped $role" || true
  done
}

migrate() {
  stop
  "$PREFIX/bin/run-role" maintenance prepare-release
}

backfill() {
  local step
  for step in "legacy-backfill 1000" "legacy-topic-backfill 1000" "legacy-event-project"; do
    say "maintenance: $step"
    "$PREFIX/bin/run-role" maintenance ${=step}
  done
  say "legacy curation import, search and hot metrics are resumable maintenance follow-ups (see README)"
}

render_agent() {
  local role=$1 target=$2
  sed -e "s#@PREFIX@#$PREFIX#g" -e "s#@ROLE@#$role#g" -e "s#@LABEL@#$LABEL_PREFIX.$role#g" \
    "$SCRIPT_DIR/launch-agent.plist.template" > "$target"
  plutil -lint -s "$target"
}

start() {
  check
  local role target
  mkdir -p "$PREFIX/logs"
  for role in web worker; do
    if [[ "${DRY_RUN:-0}" == 1 ]]; then
      target="$PREFIX/launchd-preview/$LABEL_PREFIX.$role.plist"
      mkdir -p "${target:h}"
      render_agent "$role" "$target"
      say "dry run: rendered $target"
      continue
    fi
    target=$(agent_file "$role")
    render_agent "$role" "$target"
    launchctl bootout "$DOMAIN/$LABEL_PREFIX.$role" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$target"
    say "started $role"
  done
  [[ "${DRY_RUN:-0}" == 1 ]] || status
}

status() {
  local ready=0 attempt
  for attempt in {1..18}; do
    if curl -fsS "http://127.0.0.1:$PORT/api/ready" >/dev/null 2>&1; then ready=1; break; fi
    sleep 10
  done
  (( ready )) && say "ready: http://127.0.0.1:$PORT/api/ready" || say "NOT ready after 180 s; see $PREFIX/logs"
  "$PREFIX/bin/run-role" maintenance worker-health || true
  return $(( 1 - ready ))
}

verify() {
  # Everything short of starting the loops: release, configuration of each role, database state.
  [[ "$(git -C "$PREFIX/code" rev-parse HEAD)" == "$(<"$PREFIX/RELEASE")" ]] || die "code does not match RELEASE"
  "$PREFIX/bin/run-role" worker-config > "$PREFIX/logs/worker-config.json"
  "$PREFIX/code/.venv/bin/python" - "$PREFIX/logs/worker-config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
assert config["environment"] == "production" and config["process_role"] == "worker"
assert config["allow_network_tasks"] and config["scheduler_enabled"] and config["durable_jobs_enabled"]
print("worker configuration valid:", config["environment_id"], config["database_path"])
PY
  "$PREFIX/bin/run-role" maintenance db-verify > "$PREFIX/logs/db-verify.json"
  say "database verified; web and worker configuration valid"
}

case "${1:-}" in
  check) check ;;
  verify) verify ;;
  install-code) install_code "${2:-}" ;;
  configure) configure "${2:-}" ;;
  import-legacy) import_legacy "${2:-}" ;;
  migrate) migrate ;;
  backfill) backfill ;;
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
