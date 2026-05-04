#!/usr/bin/env bash
# kedo-server.sh — clean daemon launcher for the kedo Web 服务
#
# Behavior:
#   - logs go to $KEDO_HOME/logs/kedo-<timestamp>.log
#   - $KEDO_HOME/logs/current.log symlinks to the latest log
#   - PID stored at $KEDO_HOME/server.pid
#   - start is idempotent: refuses if /api/llm/status already responds
#
# Usage:
#   kedo-server.sh                    # start in PWD (or last project_path)
#   kedo-server.sh start /path/to/proj
#   kedo-server.sh stop
#   kedo-server.sh restart /path
#   kedo-server.sh status
#   kedo-server.sh tail
#   kedo-server.sh logs               # list recent log files
#
# Env:
#   KEDO_HOME    base dir for logs/pid (default: ~/.kedo)
#   KEDO_PORT    port (default: 8000)
#   KEDO_HOST    bind host (default: 0.0.0.0)
#   KEDO_BIN     kedo binary (default: auto-detect ~/.local/bin/kedo, then `kedo` on PATH)

set -euo pipefail

KEDO_HOME="${KEDO_HOME:-$HOME/.kedo}"
LOG_DIR="$KEDO_HOME/logs"
PID_FILE="$KEDO_HOME/server.pid"
LAST_PROJECT_FILE="$KEDO_HOME/last_project_path"
PORT="${KEDO_PORT:-8000}"
HOST="${KEDO_HOST:-0.0.0.0}"

# Resolve kedo binary
if [[ -n "${KEDO_BIN:-}" ]]; then
    :
elif [[ -x "$HOME/.local/bin/kedo" ]]; then
    KEDO_BIN="$HOME/.local/bin/kedo"
elif command -v kedo >/dev/null 2>&1; then
    KEDO_BIN="$(command -v kedo)"
else
    echo "ERROR: kedo binary not found (tried \$KEDO_BIN, ~/.local/bin/kedo, PATH)" >&2
    exit 1
fi

mkdir -p "$LOG_DIR" "$KEDO_HOME"

usage() {
    sed -n '2,21p' "$0" | sed 's/^# //; s/^#//'
}

is_running() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

reachable() {
    curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/llm/status" >/dev/null 2>&1
}

current_log() {
    if [[ -L "$LOG_DIR/current.log" ]]; then
        readlink -f "$LOG_DIR/current.log" 2>/dev/null || true
    fi
}

resolve_project() {
    local p="${1:-}"
    if [[ -z "$p" ]]; then
        if [[ -f "$LAST_PROJECT_FILE" ]]; then
            p="$(cat "$LAST_PROJECT_FILE")"
        else
            p="$PWD"
        fi
    fi
    if [[ ! -d "$p" ]]; then
        echo "ERROR: project_path not a directory: $p" >&2
        exit 1
    fi
    (cd "$p" && pwd)
}

cmd_start() {
    local project_path
    project_path="$(resolve_project "${1:-}")"

    if reachable; then
        echo "kedo server already responding on :$PORT (refusing duplicate start)"
        echo "  pid: $(cat "$PID_FILE" 2>/dev/null || echo '?')"
        echo "  log: $(current_log)"
        return 0
    fi
    if is_running; then
        echo "kedo pid $(cat "$PID_FILE") still alive but port :$PORT not responding — stop first"
        return 1
    fi

    local stamp log
    stamp="$(date +%Y%m%d-%H%M%S)"
    log="$LOG_DIR/kedo-$stamp.log"
    ln -sfn "$log" "$LOG_DIR/current.log"
    echo "$project_path" > "$LAST_PROJECT_FILE"

    # 启动 — kedo 的 argparse 不支持同时给 positional project_path 和 subcommand
    # （会把 project_path 误当成 subcommand 名）。绕开方式是 cd 进项目再起 server。
    # nohup + disown 确保父 shell 退出后 daemon 继续。
    nohup bash -c "cd '$project_path' && exec '$KEDO_BIN' --port '$PORT' --host '$HOST' server" \
        > "$log" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    disown "$pid" 2>/dev/null || true

    # 探测 20 次 × 500ms = 10s
    local i
    for i in $(seq 1 20); do
        if reachable; then
            echo "kedo started:"
            echo "  pid:     $pid"
            echo "  project: $project_path"
            echo "  port:    $PORT"
            echo "  log:     $log"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "ERROR: kedo died during startup. Last 20 log lines:" >&2
            tail -n 20 "$log" >&2
            rm -f "$PID_FILE"
            return 1
        fi
        sleep 0.5
    done
    echo "WARN: kedo started (pid $pid) but /api/llm/status not responding within 10s"
    echo "  log: $log"
    return 1
}

cmd_stop() {
    if ! is_running; then
        echo "kedo not running"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    local i
    for i in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.3
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "kedo did not exit on SIGTERM, sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "kedo stopped (pid $pid)"
}

cmd_status() {
    if is_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "running"
        echo "  pid:     $pid"
        echo "  project: $(cat "$LAST_PROJECT_FILE" 2>/dev/null || echo '?')"
        echo "  port:    $PORT"
        echo "  log:     $(current_log)"
        if reachable; then
            echo "  health:  ok (/api/llm/status reachable)"
        else
            echo "  health:  PORT NOT RESPONDING"
        fi
    else
        echo "stopped"
        if [[ -f "$PID_FILE" ]]; then
            echo "  (stale pid file: $(cat "$PID_FILE")) — will be cleaned on next start"
        fi
    fi
}

cmd_tail() {
    local log
    log="$(current_log)"
    if [[ -z "$log" || ! -f "$log" ]]; then
        echo "no log yet (current.log missing)"
        return 1
    fi
    exec tail -F "$log"
}

cmd_logs() {
    ls -lt "$LOG_DIR" 2>/dev/null | head -10
}

# ----- dispatch -----
sub="${1:-start}"
case "$sub" in
    start|stop|restart|status|tail|logs) shift ;;
    -h|--help) usage; exit 0 ;;
    *) sub="start" ;;  # 第一个参数当 project_path 处理
esac

case "$sub" in
    start)   cmd_start "${1:-}" ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start "${1:-}" ;;
    status)  cmd_status ;;
    tail)    cmd_tail ;;
    logs)    cmd_logs ;;
    *)       usage; exit 2 ;;
esac
