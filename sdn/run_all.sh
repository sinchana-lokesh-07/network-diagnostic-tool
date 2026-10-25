#!/usr/bin/env bash
# Start Ryu and the Mininet diamond topology. Usage: ./run_all.sh
set -uo pipefail

SDN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${RYU_VENV:-$HOME/ryuenv}"
LOG_DIR="$SDN_DIR/logs"
OF_PORT=6653
REST_PORT=8080

if [[ ! -x "$VENV/bin/ryu-manager" ]]; then
    echo "ryu-manager not found in $VENV - create/activate the Ryu venv first"
    echo "You can override its path with RYU_VENV=/path/to/venv ./run_all.sh"
    exit 1
fi

mkdir -p "$LOG_DIR"
echo "[run_all] cleaning old Mininet/Ryu state..."
# Do not use pkill -f ryu-manager here: its pattern can match the shell command itself.
pids="$(pgrep -f '[r]yu-manager' || true)"
if [[ -n "$pids" ]]; then
    while read -r pid; do kill "$pid" 2>/dev/null || true; done <<< "$pids"
fi
sudo mn -c >/dev/null 2>&1 || true
sleep 1

echo "[run_all] starting Ryu (log: $LOG_DIR/ryu.log)..."
"$VENV/bin/ryu-manager" --observe-links \
    --ofp-tcp-listen-port "$OF_PORT" \
    --wsapi-host 127.0.0.1 --wsapi-port "$REST_PORT" \
    ryu.app.ofctl_rest ryu.app.rest_topology "$SDN_DIR/diag_controller.py" \
    > "$LOG_DIR/ryu.log" 2>&1 &
RYU_PID=$!

cleanup() {
    echo
    echo "[run_all] stopping Ryu and cleaning Mininet..."
    kill "$RYU_PID" 2>/dev/null || true
    sudo mn -c >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "[run_all] waiting for Ryu REST API on port $REST_PORT..."
UP=0
for _ in $(seq 1 30); do
    if ! kill -0 "$RYU_PID" 2>/dev/null; then
        echo "Ryu crashed - last log lines:"
        tail -n 40 "$LOG_DIR/ryu.log"
        exit 1
    fi
    if curl -fsS -o /dev/null "http://127.0.0.1:$REST_PORT/v1.0/topology/switches"; then
        UP=1
        break
    fi
    sleep 1
done
if [[ "$UP" != 1 ]]; then
    echo "Ryu REST never came up"
    tail -n 40 "$LOG_DIR/ryu.log"
    exit 1
fi

if curl -fsS -o /dev/null "http://127.0.0.1:$REST_PORT/v1.0/diag/links"; then
    echo "[run_all] congestion metrics endpoint is available."
else
    echo "WARNING: congestion metrics endpoint not responding; inspect $LOG_DIR/ryu.log"
fi

echo "[run_all] Ryu is up (pid $RYU_PID). Starting Mininet..."
sudo python3 "$SDN_DIR/topo_diamond.py"
