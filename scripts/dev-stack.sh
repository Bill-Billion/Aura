#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/output/dev-stack"
BACKEND_PID_FILE="$OUTPUT_DIR/backend.pid"
FRONTEND_PID_FILE="$OUTPUT_DIR/frontend.pid"
BACKEND_LOG="$OUTPUT_DIR/backend.log"
FRONTEND_LOG="$OUTPUT_DIR/frontend.log"
BACKEND_PORT=8000
FRONTEND_PORT=5173

mkdir -p "$OUTPUT_DIR"

runtime_path() {
  local updated_path="$PATH"
  local candidate=""

  # 设计意图：优先把宿主机 Node 所在目录放到前面，避免 Codex 内置 Node
  # 去加载 Vite / rolldown 的原生 binding 时触发签名冲突。
  for candidate in /opt/homebrew/bin /usr/local/bin; do
    if [[ -d "$candidate" ]] && [[ ":$updated_path:" != *":$candidate:"* ]]; then
      updated_path="${candidate}:${updated_path}"
    fi
  done

  printf '%s\n' "$updated_path"
}

resolve_node_bin() {
  local candidate=""

  for candidate in /opt/homebrew/bin/node /usr/local/bin/node "$(command -v node 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

load_local_env() {
  local env_file
  for env_file in "$ROOT_DIR/.env.local" "$ROOT_DIR/.env"; do
    if [[ -f "$env_file" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$env_file"
      set +a
    fi
  done
}

pid_is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  tr -d '[:space:]' < "$pid_file"
}

port_listener_pids() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
}

stop_pid_file() {
  local label="$1"
  local pid_file="$2"
  local pid=""

  pid="$(read_pid_file "$pid_file" || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$pid_file"
    return 0
  fi

  if pid_is_running "$pid"; then
    echo "[dev-stack] 停止已记录的 ${label} 进程 PID=${pid}"
    kill "$pid" 2>/dev/null || true
    for _ in {1..30}; do
      if ! pid_is_running "$pid"; then
        break
      fi
      sleep 0.2
    done
    if pid_is_running "$pid"; then
      echo "[dev-stack] ${label} 进程未及时退出，强制结束 PID=${pid}"
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi

  rm -f "$pid_file"
}

ensure_port_available() {
  local port="$1"
  local label="$2"
  local pid_file="$3"
  local recorded_pid=""
  local listeners=""

  recorded_pid="$(read_pid_file "$pid_file" || true)"
  if [[ -n "$recorded_pid" ]]; then
    stop_pid_file "$label" "$pid_file"
  fi

  listeners="$(port_listener_pids "$port" | tr '\n' ' ' | xargs || true)"
  if [[ -n "$listeners" ]]; then
    echo "[dev-stack] 端口 ${port} 仍被其他进程占用，拒绝复用。"
    echo "[dev-stack] 请先手动处理这些 PID: ${listeners}"
    exit 1
  fi
}

wait_http_ok() {
  local url="$1"
  local label="$2"

  for _ in {1..40}; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "[dev-stack] ${label} HTTP 已就绪: ${url}"
      return 0
    fi
    sleep 0.25
  done

  echo "[dev-stack] ${label} HTTP 未就绪: ${url}" >&2
  return 1
}

run_ws_check() {
  "$ROOT_DIR/backend/.venv/bin/python" "$ROOT_DIR/scripts/check_ws_keepalive.py" \
    "ws://127.0.0.1:${FRONTEND_PORT}/ws/simulation" \
    --hold-seconds 5
}

start_backend() {
  echo "[dev-stack] 启动后端 ..."
  : > "$BACKEND_LOG"
  (
    cd "$ROOT_DIR"
    load_local_env
    export PATH
    PATH="$(runtime_path)"
    python3 - "$BACKEND_PID_FILE" "$BACKEND_LOG" \
      backend/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port "$BACKEND_PORT" <<'PY'
import os
import subprocess
import sys

pid_file = sys.argv[1]
log_file = sys.argv[2]
cmd = sys.argv[3:]

with open(log_file, "ab", buffering=0) as log_fp:
    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

with open(pid_file, "w", encoding="utf-8") as pid_fp:
    pid_fp.write(str(proc.pid))
PY
  )
}

start_frontend() {
  local node_bin=""
  local vite_entry="$ROOT_DIR/frontend/node_modules/vite/bin/vite.js"

  node_bin="$(resolve_node_bin)" || {
    echo "[dev-stack] 未找到可用的宿主机 Node，可执行前端启动失败。" >&2
    exit 1
  }

  echo "[dev-stack] 启动前端 ..."
  : > "$FRONTEND_LOG"
  (
    cd "$ROOT_DIR/frontend"
    load_local_env
    export PATH
    PATH="$(runtime_path)"
    python3 - "$FRONTEND_PID_FILE" "$FRONTEND_LOG" \
      "$node_bin" "$vite_entry" --host 127.0.0.1 --port "$FRONTEND_PORT" <<'PY'
import os
import subprocess
import sys

pid_file = sys.argv[1]
log_file = sys.argv[2]
cmd = sys.argv[3:]

with open(log_file, "ab", buffering=0) as log_fp:
    proc = subprocess.Popen(
        cmd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

with open(pid_file, "w", encoding="utf-8") as pid_fp:
    pid_fp.write(str(proc.pid))
PY
  )
}

print_status() {
  local backend_pid frontend_pid
  backend_pid="$(read_pid_file "$BACKEND_PID_FILE" || true)"
  frontend_pid="$(read_pid_file "$FRONTEND_PID_FILE" || true)"

  echo "[dev-stack] backend pid: ${backend_pid:-<none>}"
  echo "[dev-stack] frontend pid: ${frontend_pid:-<none>}"
  echo "[dev-stack] backend listeners: $(port_listener_pids "$BACKEND_PORT" | tr '\n' ' ' | xargs || echo '<none>')"
  echo "[dev-stack] frontend listeners: $(port_listener_pids "$FRONTEND_PORT" | tr '\n' ' ' | xargs || echo '<none>')"
  echo "[dev-stack] logs:"
  echo "  - $BACKEND_LOG"
  echo "  - $FRONTEND_LOG"
}

verify_stack() {
  wait_http_ok "http://127.0.0.1:${BACKEND_PORT}/api/health" "后端"
  wait_http_ok "http://127.0.0.1:${FRONTEND_PORT}" "前端"
  run_ws_check
}

start_stack() {
  ensure_port_available "$BACKEND_PORT" "backend" "$BACKEND_PID_FILE"
  ensure_port_available "$FRONTEND_PORT" "frontend" "$FRONTEND_PID_FILE"
  start_backend
  start_frontend
  verify_stack
  print_status
}

stop_stack() {
  stop_pid_file "backend" "$BACKEND_PID_FILE"
  stop_pid_file "frontend" "$FRONTEND_PID_FILE"
  print_status
}

case "${1:-start}" in
  start)
    start_stack
    ;;
  stop)
    stop_stack
    ;;
  restart)
    stop_stack
    start_stack
    ;;
  status)
    print_status
    ;;
  verify)
    verify_stack
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|verify}" >&2
    exit 1
    ;;
esac
