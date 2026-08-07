#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_HELPER="${ROOT_DIR}/scripts/run_web_config.py"

if [[ "${VLA_RUN_WEB_CONFIG_LOADED:-}" != "1" ]]; then
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    CONFIG_PYTHON="${ROOT_DIR}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    CONFIG_PYTHON="$(command -v python3)"
  else
    echo "Python 3 is required to load the Web deployment configuration." >&2
    exit 127
  fi
  exec "${CONFIG_PYTHON}" "${CONFIG_HELPER}" -- \
    bash "${SCRIPT_DIR}/run_web.sh" "$@"
fi

ACTION="${1:-start}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
STATE_DIR="${STATE_DIR:-${ROOT_DIR}/.djx}"
WORKING_DIR_WAS_SET=0
WEB_WORKING_DIR_WAS_SET=0
if [[ "${WORKING_DIR+x}" == "x" ]]; then
  WORKING_DIR_WAS_SET=1
fi
if [[ "${VLA_DATA_AGENT_WEB_WORKING_DIR+x}" == "x" ]]; then
  WEB_WORKING_DIR_WAS_SET=1
fi
WORKING_DIR="${WORKING_DIR-}"
PID_FILE="${PID_FILE:-${STATE_DIR}/web.pid}"
LOG_DIR="${LOG_DIR:-${STATE_DIR}/logs}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/web.log}"
FRONTEND_DIST="${FRONTEND_DIST:-${ROOT_DIR}/frontend/dist}"
SKIP_FRONTEND_BUILD="${SKIP_FRONTEND_BUILD:-0}"
VLA_FRONTEND_NODE_BIN_DIR="${VLA_FRONTEND_NODE_BIN_DIR:-}"
VLA_VLADATASETS_ROOT="${VLA_VLADATASETS_ROOT:-/media/heying/hy_data1/VLADatasets}"
WEB_CMD="${WEB_CMD:-}"
CONTROL_PYTHON="${RUN_WEB_CONTROL_PYTHON:-}"
CONTROL_HELPER="${ROOT_DIR}/scripts/run_web_control.py"
REQUIRED_FRONTEND_NODE_VERSION="24.18.0"
REQUIRED_FRONTEND_NPM_VERSION="11.16.0"

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
  WORKING_DIR           Authoritative --working-dir value. Default: STATE_DIR
  VLA_DATA_AGENT_WEB_WORKING_DIR
                        Used when WORKING_DIR is unset. Must match it when both are set.
  FRONTEND_DIST         Built frontend directory. Default: frontend/dist
  STATE_DIR             State directory for PID and logs. Default: .djx
  PID_FILE              PID file path. Default: .djx/web.pid
  LOG_DIR               Log directory. Default: STATE_DIR/logs
  LOG_FILE              Log file path. Default: .djx/logs/web.log
  SKIP_FRONTEND_BUILD   Set to 1 to reuse existing frontend/dist.
  VLA_FRONTEND_NODE_BIN_DIR
                        Optional directory containing the required Node.js and npm binaries.
                        It is prepended to PATH only for the frontend build.
  VLA_TRAINING_DEV_ADMIN
                        Set to 1 only for local simulation write access. Default: 0.
  VLA_TRAINING_SIMULATION_ENABLED
                        Enable the Fake Runner simulation API. Default: 1.
  VLA_TRAINING_FAKE_TICK_SECONDS
                        Delay between simulated training metrics. Default: 0.25.
  VLA_TRAINING_DB_PATH
                        Optional absolute training SQLite path. Default: WORKING_DIR/training.sqlite.
  WEB_CMD               Override vla-data-agent-web command path.
  RUN_WEB_CONTROL_PYTHON
                        Python 3 used for safe PID/control operations.

Before evaluating these settings, the script loads the optional fixed
~/.config/vla-data-juicer-agents/run-web.json as inert JSON. The file must be
owned by the current user with mode 0600, inside an owner-controlled directory
with mode 0700. Explicit values already present in the calling environment take
precedence. DASHSCOPE_API_KEY is never read from this file and must be inherited
from the calling shell.

New WORKING_DIR, STATE_DIR, and LOG_DIR paths are created under umask 077.
Existing directory permissions are not changed.
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

resolve_control_python() {
  if [[ -n "${CONTROL_PYTHON}" ]]; then
    return
  fi
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    CONTROL_PYTHON="${ROOT_DIR}/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    CONTROL_PYTHON="$(command -v python3)"
    return
  fi
  echo "Python 3 is required for safe Web service lifecycle control." >&2
  return 127
}

control_helper() {
  resolve_control_python
  "${CONTROL_PYTHON}" "${CONTROL_HELPER}" "$@"
}

run_locked_control_action() {
  local internal_action="$1"
  resolve_control_python
  "${CONTROL_PYTHON}" "${CONTROL_HELPER}" \
    with-lock \
    --pid-file "${PID_FILE}" \
    -- \
    bash "${SCRIPT_DIR}/run_web.sh" "${internal_action}"
}

require_locked_control_action() {
  if [[ "${VLA_RUN_WEB_CONTROL_LOCKED:-}" != "1" ]]; then
    echo "Internal Web lifecycle actions require the control lock." >&2
    return 2
  fi
  local verify_status
  if control_helper verify-lock --pid-file "${PID_FILE}"; then
    :
  else
    verify_status=$?
    return "${verify_status}"
  fi
  local descriptor_name
  local descriptor
  for descriptor_name in \
    VLA_RUN_WEB_ANCHOR_FD \
    VLA_RUN_WEB_PARENT_FD \
    VLA_RUN_WEB_CONTROL_FD; do
    descriptor="${!descriptor_name:-}"
    if [[ ! "${descriptor}" =~ ^[0-9]+$ ]] || (( descriptor <= 2 )); then
      echo "Internal Web lifecycle lock capability is invalid." >&2
      return 2
    fi
    eval "exec ${descriptor}>&-"
    unset "${descriptor_name}"
  done
}

read_service_pid() {
  control_helper read-pid --pid-file "${PID_FILE}"
}

active_service_pid() {
  local expected_pid="${1:-}"
  if [[ -n "${expected_pid}" ]]; then
    control_helper \
      active-pid \
      --pid-file "${PID_FILE}" \
      --expected-pid "${expected_pid}"
    return
  fi
  control_helper active-pid --pid-file "${PID_FILE}"
}

write_service_pid() {
  local pid="$1"
  control_helper write-pid --pid-file "${PID_FILE}" --pid "${pid}"
}

remove_service_record() {
  local expected_pid="$1"
  control_helper \
    remove-service \
    --pid-file "${PID_FILE}" \
    --expected-pid "${expected_pid}"
}

prepare_service_start() {
  control_helper prepare-start --pid-file "${PID_FILE}"
}

signal_service_instance() {
  local expected_pid="$1"
  local signal_name="$2"
  control_helper \
    signal-instance \
    --pid-file "${PID_FILE}" \
    --expected-pid "${expected_pid}" \
    --signal "${signal_name}"
}

remove_stopped_service_record() {
  local expected_pid="$1"
  local remove_status
  if remove_service_record "${expected_pid}"; then
    return 0
  else
    remove_status=$?
  fi
  if [[ "${remove_status}" == "3" ]]; then
    return 0
  fi
  echo "PID/instance records changed and were not removed." >&2
  return 1
}

resolve_working_directory() {
  if [[ "${WORKING_DIR_WAS_SET}" == "1" \
    && "${WEB_WORKING_DIR_WAS_SET}" == "1" \
    && "${WORKING_DIR}" != "${VLA_DATA_AGENT_WEB_WORKING_DIR-}" ]]; then
    echo "WORKING_DIR and VLA_DATA_AGENT_WEB_WORKING_DIR must match when both are set." >&2
    return 2
  fi
  if [[ "${WORKING_DIR_WAS_SET}" != "1" ]]; then
    if [[ "${WEB_WORKING_DIR_WAS_SET}" == "1" ]]; then
      WORKING_DIR="${VLA_DATA_AGENT_WEB_WORKING_DIR}"
    else
      WORKING_DIR="${STATE_DIR}"
    fi
  fi
  if [[ -z "${WORKING_DIR}" ]]; then
    echo "The effective web working directory must not be empty." >&2
    return 2
  fi
}

prepare_state_directories() {
  # Apply private creation modes without changing permissions on existing paths.
  umask 077
  mkdir -p "${STATE_DIR}" "${LOG_DIR}"
}

prepare_runtime_directories() {
  resolve_working_directory
  prepare_state_directories
  mkdir -p "${WORKING_DIR}"
}

build_frontend() {
  if [[ "${SKIP_FRONTEND_BUILD}" == "1" ]]; then
    echo "Skipping frontend build because SKIP_FRONTEND_BUILD=1."
    return
  fi

  echo "Building frontend..."
  (
    if [[ -n "${VLA_FRONTEND_NODE_BIN_DIR}" ]]; then
      if [[ "${VLA_FRONTEND_NODE_BIN_DIR}" != /* ]]; then
        echo "VLA_FRONTEND_NODE_BIN_DIR must be an absolute directory." >&2
        return 2
      fi
      if [[ ! -d "${VLA_FRONTEND_NODE_BIN_DIR}" ]]; then
        echo "VLA_FRONTEND_NODE_BIN_DIR is not an existing directory: ${VLA_FRONTEND_NODE_BIN_DIR}" >&2
        return 2
      fi
      if [[ ! -x "${VLA_FRONTEND_NODE_BIN_DIR}/node" \
        || ! -x "${VLA_FRONTEND_NODE_BIN_DIR}/npm" ]]; then
        echo "VLA_FRONTEND_NODE_BIN_DIR must contain executable node and npm binaries." >&2
        return 2
      fi
      export PATH="${VLA_FRONTEND_NODE_BIN_DIR}:${PATH}"
    fi

    local node_version
    local npm_version
    if ! command -v node >/dev/null 2>&1; then
      echo "Node.js ${REQUIRED_FRONTEND_NODE_VERSION} is required to build the frontend." >&2
      echo "Run 'nvm install' and 'nvm use', or set VLA_FRONTEND_NODE_BIN_DIR." >&2
      return 127
    fi
    if ! command -v npm >/dev/null 2>&1; then
      echo "npm ${REQUIRED_FRONTEND_NPM_VERSION} is required to build the frontend." >&2
      echo "Run 'nvm install' and 'nvm use', or set VLA_FRONTEND_NODE_BIN_DIR." >&2
      return 127
    fi
    node_version="$(node --version 2>/dev/null || true)"
    node_version="${node_version#v}"
    npm_version="$(npm --version 2>/dev/null || true)"
    if [[ "${node_version}" != "${REQUIRED_FRONTEND_NODE_VERSION}" \
      || "${npm_version}" != "${REQUIRED_FRONTEND_NPM_VERSION}" ]]; then
      echo "Frontend build toolchain mismatch." >&2
      echo "Required: Node.js ${REQUIRED_FRONTEND_NODE_VERSION}, npm ${REQUIRED_FRONTEND_NPM_VERSION}." >&2
      echo "Detected: Node.js ${node_version:-unavailable}, npm ${npm_version:-unavailable}." >&2
      echo "Run 'nvm install' and 'nvm use', or set VLA_FRONTEND_NODE_BIN_DIR." >&2
      echo "Set SKIP_FRONTEND_BUILD=1 only when reusing a verified existing frontend/dist." >&2
      return 2
    fi

    cd "${ROOT_DIR}/frontend"
    npm run build
  )
}

web_command() {
  resolve_web_cmd
  prepare_runtime_directories
  export VLA_VLADATASETS_ROOT VLA_DATA_AGENT_WEB_WORKING_DIR="${WORKING_DIR}"
  "${WEB_CMD}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --working-dir "${WORKING_DIR}" \
    --frontend-dist "${FRONTEND_DIST}"
}

start_service() {
  local current_pid
  local current_status
  if current_pid="$(active_service_pid)"; then
    echo "DataPilot web service is already running with PID ${current_pid}."
    echo "URL: http://${HOST}:${PORT}"
    return
  else
    current_status=$?
  fi
  if [[ "${current_status}" != "3" && "${current_status}" != "5" ]]; then
    return "${current_status}"
  fi
  if [[ "${current_status}" == "5" ]]; then
    local stale_pid
    local stale_status
    if stale_pid="$(read_service_pid)"; then
      if remove_service_record "${stale_pid}"; then
        :
      else
        stale_status=$?
        echo "Stale Web instance records are still owned; start is blocked." >&2
        return "${stale_status}"
      fi
    else
      stale_status=$?
      if [[ "${stale_status}" != "3" ]]; then
        return "${stale_status}"
      fi
    fi
  fi
  local prepare_status
  if prepare_service_start; then
    :
  else
    prepare_status=$?
    echo "Stale Web instance records are still owned; start is blocked." >&2
    return "${prepare_status}"
  fi

  resolve_working_directory
  build_frontend
  resolve_web_cmd
  resolve_control_python
  prepare_runtime_directories

  echo "Starting DataPilot web service..."
  export VLA_VLADATASETS_ROOT VLA_DATA_AGENT_WEB_WORKING_DIR="${WORKING_DIR}"
  nohup "${CONTROL_PYTHON}" "${CONTROL_HELPER}" \
    hold-instance \
    --pid-file "${PID_FILE}" \
    -- \
    "${WEB_CMD}" \
      --host "${HOST}" \
      --port "${PORT}" \
      --working-dir "${WORKING_DIR}" \
      --frontend-dist "${FRONTEND_DIST}" \
    >"${LOG_FILE}" 2>&1 &
  local pid=$!
  if ! write_service_pid "${pid}"; then
    echo "The Web service PID could not be recorded safely; stopping the new process." >&2
    kill -- "${pid}" >/dev/null 2>&1 || true
    wait "${pid}" >/dev/null 2>&1 || true
    return 1
  fi

  sleep 1
  local verified_pid
  local active_status
  if verified_pid="$(active_service_pid "${pid}")"; then
    if [[ "${verified_pid}" == "${pid}" ]]; then
      echo "DataPilot web service started with PID ${pid}."
      echo "URL: http://${HOST}:${PORT}"
      echo "Log: ${LOG_FILE}"
      return
    fi
    active_status=4
  else
    active_status=$?
  fi

  if [[ "${active_status}" != "0" ]]; then
    echo "DataPilot web service failed to start. Log: ${LOG_FILE}" >&2
    tail -n 80 "${LOG_FILE}" >&2 || true
    kill -- "${pid}" >/dev/null 2>&1 || true
    wait "${pid}" >/dev/null 2>&1 || true
    local remove_status
    if remove_service_record "${pid}"; then
      :
    else
      remove_status=$?
      if [[ "${remove_status}" != "3" ]]; then
        echo "PID file changed while startup failed; it was not removed." >&2
      fi
    fi
    exit 1
  fi
}

stop_service() {
  local pid
  local active_status
  if pid="$(active_service_pid)"; then
    active_status=0
  else
    active_status=$?
  fi

  if [[ "${active_status}" == "3" ]]; then
    echo "DataPilot web service is not running."
    return
  fi
  if [[ "${active_status}" == "5" ]]; then
    if pid="$(read_service_pid)"; then
      echo "DataPilot web service is not running. Removing stale PID record."
      remove_stopped_service_record "${pid}"
      return
    else
      active_status=$?
    fi
    return "${active_status}"
  fi
  if [[ "${active_status}" != "0" ]]; then
    return "${active_status}"
  fi

  echo "Stopping DataPilot web service with PID ${pid}..."
  local signal_status
  if signal_service_instance "${pid}" "TERM"; then
    :
  else
    signal_status=$?
    if [[ "${signal_status}" != "3" && "${signal_status}" != "5" ]]; then
      return "${signal_status}"
    fi
  fi

  for _ in {1..30}; do
    local verified_pid
    if verified_pid="$(active_service_pid "${pid}")"; then
      if [[ "${verified_pid}" != "${pid}" ]]; then
        echo "Web service instance identity changed while stopping." >&2
        return 1
      fi
    else
      active_status=$?
      if [[ "${active_status}" == "3" || "${active_status}" == "5" ]]; then
        remove_stopped_service_record "${pid}"
        echo "DataPilot web service stopped."
        return
      fi
      return "${active_status}"
    fi
    sleep 1
  done

  echo "Service did not stop after 30 seconds; forcing stop."
  if signal_service_instance "${pid}" "KILL"; then
    :
  else
    signal_status=$?
    if [[ "${signal_status}" != "3" && "${signal_status}" != "5" ]]; then
      return "${signal_status}"
    fi
  fi

  for _ in {1..50}; do
    if active_service_pid "${pid}" >/dev/null; then
      sleep 0.1
      continue
    fi
    active_status=$?
    if [[ "${active_status}" == "3" || "${active_status}" == "5" ]]; then
      remove_stopped_service_record "${pid}"
      echo "DataPilot web service stopped."
      return
    fi
    return "${active_status}"
  done
  echo "Web service instance remains active after forced stop." >&2
  return 1
}

status_service() {
  local pid
  local active_status
  if pid="$(active_service_pid)"; then
    echo "DataPilot web service is running with PID ${pid}."
    echo "URL: http://${HOST}:${PORT}"
    return 0
  else
    active_status=$?
  fi
  if [[ "${active_status}" == "5" ]]; then
    echo "DataPilot web service is not running. Stale PID file: ${PID_FILE}"
    return 1
  fi
  if [[ "${active_status}" != "3" ]]; then
    return "${active_status}"
  fi

  echo "DataPilot web service is not running."
  return 3
}

locked_restart_service() {
  resolve_working_directory
  stop_service
  start_service
}

case "${ACTION}" in
  -h|--help|help)
    usage
    ;;
  start)
    resolve_working_directory
    run_locked_control_action "__control_start"
    ;;
  stop)
    run_locked_control_action "__control_stop"
    ;;
  restart)
    resolve_working_directory
    run_locked_control_action "__control_restart"
    ;;
  status)
    run_locked_control_action "__control_status"
    ;;
  __control_start)
    require_locked_control_action
    start_service
    ;;
  __control_stop)
    require_locked_control_action
    stop_service
    ;;
  __control_restart)
    require_locked_control_action
    locked_restart_service
    ;;
  __control_status)
    require_locked_control_action
    status_service
    ;;
  logs)
    prepare_state_directories
    touch "${LOG_FILE}"
    tail -n 80 -f "${LOG_FILE}"
    ;;
  foreground)
    resolve_working_directory
    build_frontend
    web_command
    ;;
  *)
    echo "Unknown command: ${ACTION}" >&2
    usage >&2
    exit 2
    ;;
esac
