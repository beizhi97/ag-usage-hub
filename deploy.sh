#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

mkdir -p "$APP_DIR/data"

# 1. Ensure .env exists and has a 32-char random AG_ADMIN_KEY
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env" 2>/dev/null || touch "$APP_DIR/.env"
fi

generate_random_key() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 16
    elif [ -r /dev/urandom ]; then
        tr -dc 'a-f0-9' < /dev/urandom | head -c 32
    else
        date +%s%N | sha256sum | head -c 32
    fi
}

CURRENT_KEY="$(grep -E '^AG_ADMIN_KEY=' "$APP_DIR/.env" | cut -d'=' -f2- | tr -d '[:space:]"'\''' || true)"
if [ -z "$CURRENT_KEY" ]; then
    CURRENT_KEY="$(generate_random_key)"
    if grep -qE '^AG_ADMIN_KEY=' "$APP_DIR/.env"; then
        sed -i.bak "s|^AG_ADMIN_KEY=.*|AG_ADMIN_KEY=${CURRENT_KEY}|" "$APP_DIR/.env" && rm -f "$APP_DIR/.env.bak"
    else
        echo "AG_ADMIN_KEY=${CURRENT_KEY}" >> "$APP_DIR/.env"
    fi
fi

# Persist to data/admin.key as well for consistency
echo -n "$CURRENT_KEY" > "$APP_DIR/data/admin.key"
chmod 600 "$APP_DIR/data/admin.key" 2>/dev/null || true

AG_BIND="$(grep -E '^AG_BIND=' "$APP_DIR/.env" | cut -d'=' -f2- | tr -d '[:space:]"'\''' || true)"
if [ -z "$AG_BIND" ]; then
    AG_BIND="127.0.0.1:8787"
fi
BIND_HOST="${AG_BIND%:*}"
BIND_PORT="${AG_BIND##*:}"
if [ "$BIND_HOST" = "$BIND_PORT" ]; then
    BIND_HOST="127.0.0.1"
    BIND_PORT="8787"
fi

echo "========================================================"
echo " Deploying ag-usage-hub (Antigravity Token Telemetry Hub)"
echo "========================================================"

# 2. Deploy via Docker Compose or fallback to Python venv + systemd
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "[*] Docker & Docker Compose detected. Starting container..."
    AG_BIND="$AG_BIND" docker compose up -d --build
else
    echo "[*] Docker Compose not available. Falling back to python3 venv + systemd..."
    if ! command -v python3 >/dev/null 2>&1; then
        echo "[!] Error: python3 is required when Docker is not installed." >&2
        exit 1
    fi

    if [ ! -d "$APP_DIR/.venv" ]; then
        python3 -m venv "$APP_DIR/.venv"
    fi
    "$APP_DIR/.venv/bin/pip" install --upgrade pip
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

    SERVICE_FILE="/etc/systemd/system/ag-usage-hub.service"
    if [ "$(id -u)" -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
        cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Antigravity Usage Hub Telemetry Server
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn server.main:app --host ${BIND_HOST} --port ${BIND_PORT} --proxy-headers --forwarded-allow-ips=*
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable --now ag-usage-hub.service
        systemctl restart ag-usage-hub.service
    elif command -v sudo >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
        sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Antigravity Usage Hub Telemetry Server
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn server.main:app --host ${BIND_HOST} --port ${BIND_PORT} --proxy-headers --forwarded-allow-ips=*
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable --now ag-usage-hub.service
        sudo systemctl restart ag-usage-hub.service
    else
        echo "[*] systemctl not accessible; starting uvicorn in background via nohup..."
        pkill -f "uvicorn server.main:app" 2>/dev/null || true
        nohup "$APP_DIR/.venv/bin/uvicorn" server.main:app \
            --host "$BIND_HOST" --port "$BIND_PORT" \
            --proxy-headers --forwarded-allow-ips="*" \
            > "$APP_DIR/data/server.log" 2>&1 &
    fi
fi

# 3. Poll health endpoint
HEALTH_URL="http://127.0.0.1:${BIND_PORT}/health"
echo "[*] Waiting for service health check at ${HEALTH_URL} ..."
HEALTH_OK=0
for _ in $(seq 1 30); do
    if curl -fsSL "$HEALTH_URL" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

if [ "$HEALTH_OK" -ne 1 ]; then
    echo "[!] Warning: Health check at ${HEALTH_URL} did not respond within 30s. Check logs." >&2
else
    echo "[+] Health check passed!"
fi

cat <<EOF

========================================================
 ✅ ag-usage-hub Deployed Successfully!
========================================================
Local Listen Address : http://${BIND_HOST}:${BIND_PORT}
Health Endpoint      : ${HEALTH_URL}
Admin Key (AG_ADMIN_KEY):
  ${CURRENT_KEY}

--- Nginx Reverse Proxy Snippet Example ---
server {
    listen 80;
    server_name ag.example.com;

    location / {
        proxy_pass http://127.0.0.1:${BIND_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
    }
}
========================================================
EOF
