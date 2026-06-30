#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ACTION="${1:-start}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
STATE_DIR="${STATE_DIR:-${ROOT_DIR}/.djx}"
WORKING_DIR="${WORKING_DIR:-${STATE_DIR}}"
PID_FILE="${PID_FILE:-${STATE_DIR}/web.pid}"
LOG_DIR="${LOG_DIR:-${STATE_DIR}/logs}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/web.log}"
FRONTEND_DIST="${FRONTEND_DIST:-${ROOT_DIR}/frontend/dist}"
SKIP_FRONTEND_BUILD="${SKIP_FRONTEND_BUILD:-0}"
VLA_VLADATASETS_ROOT="${VLA_VLADATASETS_ROOT:-/media/heying/hy_data1/VLADatasets}"
WEB_CMD="${WEB_CMD:-}"

usage() {
  cat <<USAGE
Usage: scripts/run_web.sh [start|stop|restart|status|logs|foreground]

Commands:
  start       Build the frontend and start the web service in the background. This is the default.
  stop        Stop the background web service recorded in the PID file.
  restart     Stop the service, then start it again.
  status      Report whether the service is running.
  logs        Print the current service log and follow new lines.
  foreground  Build the frontend and run the web service in the foreground.

Environment:
  HOST                  Bind host. Default: 0.0.0.0
  PORT                  Bind port. Default: 8765
  VLA_VLADATASETS_ROOT  Dataset root. Default: /media/heying/hy_data1/VLADatasets
  WORKING_DIR           Web working directory. Default: .djx
  FRONTEND_DIST         Built frontend directory. Default: frontend/dist
  STATE_DIR             State directory for PID and logs. Default: .djx
  PID_FILE              PID file path. Default: .djx/web.pid
  LOG_FILE              Log file path. Default: .djx/logs/web.log
  SKIP_FRONTEND_BUILD   Set to 1 to reuse existing frontend/dist.
  WEB_CMD               Override vla-data-agent-web command path.
USAGE
}

resolve_web_cmd() {
  if [[ -n "${WEB_CMD}" ]]; then
    return
  fi

  if command -v vla-data-agent-web >/dev/null 2>&1; then
    WEB_CMD="$(command -v vla-data-agent-web)"
    return
  fi

  if [[ -x "${ROOT_DIR}/.venv/bin/vla-data-agent-web" ]]; then
    WEB_CMD="${ROOT_DIR}/.venv/bin/vla-data-agent-web"
    return
  fi

  echo "vla-data-agent-web was not found. Activate the Python environment or set WEB_CMD." >&2
  exit 127
}

is_running() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(cat "${PID_FILE}")"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" >/dev/null 2>&1
}

build_frontend() {
  if [[ "${SKIP_FRONTEND_BUILD}" == "1" ]]; then
    echo "Skipping frontend build because SKIP_FRONTEND_BUILD=1."
    return
  fi

  echo "Building frontend..."
  (cd "${ROOT_DIR}/frontend" && npm run build)
}

web_command() {
  resolve_web_cmd
  export VLA_VLADATASETS_ROOT
  mkdir -p "${WORKING_DIR}" "${LOG_DIR}"
  "${WEB_CMD}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --working-dir "${WORKING_DIR}" \
    --frontend-dist "${FRONTEND_DIST}"
}

start_service() {
  if is_running; then
    echo "DataPilot web service is already running with PID $(cat "${PID_FILE}")."
    echo "URL: http://${HOST}:${PORT}"
    return
  fi

  build_frontend
  resolve_web_cmd
  mkdir -p "${WORKING_DIR}" "${LOG_DIR}"

  echo "Starting DataPilot web service..."
  export VLA_VLADATASETS_ROOT
  nohup "${WEB_CMD}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --working-dir "${WORKING_DIR}" \
    --frontend-dist "${FRONTEND_DIST}" \
    >"${LOG_FILE}" 2>&1 &
  local pid=$!
  echo "${pid}" >"${PID_FILE}"

  sleep 1
  if ! kill -0 "${pid}" >/dev/null 2>&1; then
    echo "DataPilot web service failed to start. Log: ${LOG_FILE}" >&2
    tail -n 80 "${LOG_FILE}" >&2 || true
    rm -f "${PID_FILE}"
    exit 1
  fi

  echo "DataPilot web service started with PID ${pid}."
  echo "URL: http://${HOST}:${PORT}"
  echo "Log: ${LOG_FILE}"
}

stop_service() {
  if [[ ! -f "${PID_FILE}" ]]; then
    echo "DataPilot web service is not running."
    return
  fi

  local pid
  pid="$(cat "${PID_FILE}")"
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" >/dev/null 2>&1; then
    echo "DataPilot web service is not running. Removing stale PID file."
    rm -f "${PID_FILE}"
    return
  fi

  echo "Stopping DataPilot web service with PID ${pid}..."
  kill "${pid}"
  for _ in {1..30}; do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      rm -f "${PID_FILE}"
      echo "DataPilot web service stopped."
      return
    fi
    sleep 1
  done

  echo "Service did not stop after 30 seconds; forcing stop."
  kill -9 "${pid}" >/dev/null 2>&1 || true
  rm -f "${PID_FILE}"
  echo "DataPilot web service stopped."
}

status_service() {
  if is_running; then
    echo "DataPilot web service is running with PID $(cat "${PID_FILE}")."
    echo "URL: http://${HOST}:${PORT}"
    return 0
  fi

  if [[ -f "${PID_FILE}" ]]; then
    echo "DataPilot web service is not running. Stale PID file: ${PID_FILE}"
    return 1
  fi

  echo "DataPilot web service is not running."
  return 3
}

case "${ACTION}" in
  -h|--help|help)
    usage
    ;;
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    status_service
    ;;
  logs)
    mkdir -p "${LOG_DIR}"
    touch "${LOG_FILE}"
    tail -n 80 -f "${LOG_FILE}"
    ;;
  foreground)
    build_frontend
    web_command
    ;;
  *)
    echo "Unknown command: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
