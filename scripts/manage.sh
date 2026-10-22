#!/usr/bin/env bash
#
# sky-forecast local preview manager
#
#   scripts/manage.sh start | stop | status | restart
#
# Runs `scripts/serve.py --port 8080 --no-browser` as a background daemon,
# tracking its PID under .run/serve.pid and logging to .run/serve.log.
#
# Environment overrides:
#   SKY_FORECAST_HOST  bind address          (default: 127.0.0.1)
#   SKY_FORECAST_PORT  TCP port              (default: 8080)
#   PYTHON             interpreter to use    (default: python3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVE_PY="${SCRIPT_DIR}/serve.py"

HOST="${SKY_FORECAST_HOST:-127.0.0.1}"
PORT="${SKY_FORECAST_PORT:-8080}"
PYTHON="${PYTHON:-python3}"

RUN_DIR="${PROJECT_ROOT}/.run"
PID_FILE="${RUN_DIR}/serve.pid"
LOG_FILE="${RUN_DIR}/serve.log"

INFO_PREFIX="[manage]"

log()  { printf '%s %s\n' "${INFO_PREFIX}" "$*"; }
die()  { printf '%s error: %s\n' "${INFO_PREFIX}" "$*" >&2; exit 1; }

is_alive() {
    local pid="${1:-}"
    [ -n "${pid}" ] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    # A reaped-pending zombie still answers kill -0; treat it as stopped.
    if [ -r "/proc/${pid}/stat" ]; then
        [ "$(awk '{print $3}' "/proc/${pid}/stat" 2>/dev/null)" != "Z" ] || return 1
    fi
    return 0
}

is_running() {
    [ -f "${PID_FILE}" ] || return 1
    is_alive "$(cat "${PID_FILE}" 2>/dev/null || true)"
}

port_busy() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
    else
        (exec 3<>"/dev/tcp/${HOST}/${PORT}") 2>/dev/null && exec 3>&- && return 0 || return 1
    fi
}

ensure_runtime() {
    mkdir -p "${RUN_DIR}"
}

start() {
    ensure_runtime
    if is_running; then
        log "already running (pid $(cat "${PID_FILE}")) on http://${HOST}:${PORT}/"
        return 0
    fi
    if port_busy; then
        die "port ${PORT} is already in use by another process"
    fi
    command -v "${PYTHON}" >/dev/null 2>&1 || die "python interpreter '${PYTHON}' not found"
    [ -f "${SERVE_PY}" ] || die "serve script not found: ${SERVE_PY}"

    log "starting preview daemon on http://${HOST}:${PORT}/"
    cd "${PROJECT_ROOT}"
    nohup "${PYTHON}" "${SERVE_PY}" --host "${HOST}" --port "${PORT}" --no-browser \
        >>"${LOG_FILE}" 2>&1 &
    local pid=$!
    printf '%s\n' "${pid}" >"${PID_FILE}"

    local i
    for i in $(seq 1 50); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            rm -f "${PID_FILE}"
            die "daemon exited during startup; see ${LOG_FILE}"
        fi
        if port_busy; then
            log "started (pid ${pid}); log: ${LOG_FILE}"
            return 0
        fi
        sleep 0.1
    done
    log "started (pid ${pid}), but port ${PORT} did not respond yet; see ${LOG_FILE}"
}

stop() {
    if ! is_running; then
        log "not running"
        rm -f "${PID_FILE}"
        return 0
    fi
    local pid
    pid="$(cat "${PID_FILE}")"
    log "stopping (pid ${pid})"
    kill "${pid}" 2>/dev/null || true
    local i
    for i in $(seq 1 50); do
        is_alive "${pid}" || break
        sleep 0.1
    done
    if is_alive "${pid}"; then
        log "forcing kill"
        kill -9 "${pid}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
    log "stopped"
}

status() {
    if is_running; then
        local pid
        pid="$(cat "${PID_FILE}")"
        printf '%s running (pid %s) on http://%s:%s/\n' "${INFO_PREFIX}" "${pid}" "${HOST}" "${PORT}"
    else
        printf '%s stopped\n' "${INFO_PREFIX}"
        return 1
    fi
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    status)  status ;;
    restart) stop; start ;;
    *)
        printf 'usage: %s {start|stop|status|restart}\n' "$(basename "$0")" >&2
        exit 2
        ;;
esac
