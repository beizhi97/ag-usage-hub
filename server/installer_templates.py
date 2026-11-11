"""Dynamic installer script templates (.sh and .ps1) for ag-usage-hub clients."""


def build_agent_prompt(origin: str, api_token: str) -> str:
    clean_origin = origin.strip().rstrip("/")
    return (
        f'请在本机安装 Antigravity Token 监控探针并开启开机保活与断点补传：'
        f'Linux/macOS 执行 curl -fsSL "{clean_origin}/i/{api_token}.sh" | bash ；'
        f'Windows 在 PowerShell 执行 irm "{clean_origin}/i/{api_token}.ps1" | iex ，'
        f'完成后验证首次历史数据上报成功。'
    )


_SH_TEMPLATE = """#!/usr/bin/env bash
# ==============================================================================
# Antigravity Usage Hub - Client Auto-Installer & Sync Daemon (Linux / macOS)
# Device: __DISPLAY_NAME__ (__DEVICE_ID__)
# ==============================================================================
set -euo pipefail

BASE_URL="__BASE_URL__"
API_URL="__API_URL__"
API_TOKEN="__API_TOKEN__"
INSTALL_DIR="$HOME/.ag-usage-hub"
SYNC_SCRIPT="$INSTALL_DIR/sync.sh"

mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"

cat > "$SYNC_SCRIPT" <<'EOSYNC'
#!/usr/bin/env bash
set -euo pipefail

BASE_URL="__BASE_URL__"
API_URL="__API_URL__"
API_TOKEN="__API_TOKEN__"
INSTALL_DIR="$HOME/.ag-usage-hub"
STATE_FILE="$INSTALL_DIR/last_sync_date"
LOCK_DIR="$INSTALL_DIR/sync.lock"
LOG_FILE="$INSTALL_DIR/sync.log"

# 1. Enrich PATH for non-interactive shells (cron / systemd / launchd)
export PATH="$HOME/.bun/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -d "$HOME/.nvm/versions/node" ]; then
    LATEST_NVM_NODE="$(ls -1 "$HOME/.nvm/versions/node" 2>/dev/null | tail -n 1 || true)"
    if [ -n "$LATEST_NVM_NODE" ] && [ -d "$HOME/.nvm/versions/node/$LATEST_NVM_NODE/bin" ]; then
        export PATH="$HOME/.nvm/versions/node/$LATEST_NVM_NODE/bin:$PATH"
    fi
fi

# 2. Concurrency lock (flock or atomic mkdir with stale check)
cleanup_lock() {
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
if command -v flock >/dev/null 2>&1; then
    exec 9>"$INSTALL_DIR/sync.flock"
    if ! flock -n 9; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Another sync is already running. Exiting."
        exit 0
    fi
else
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +15 2>/dev/null || true)" ]; then
            rmdir "$LOCK_DIR" 2>/dev/null || true
            mkdir "$LOCK_DIR"
        else
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Another sync is already running. Exiting."
            exit 0
        fi
    fi
    trap cleanup_lock EXIT
fi

# 3. Detect runner: bunx or npx --yes
if command -v bunx >/dev/null 2>&1; then
    RUNNER_CMD=(bunx)
elif command -v npx >/dev/null 2>&1; then
    RUNNER_CMD=(npx --yes)
else
    echo "[!] Error: Neither bunx nor npx found in PATH ($PATH)." >&2
    exit 1
fi

# 4. Compute --since argument from watermark last_sync_date (1 day lookback)
SINCE_ARGS=()
if [ -f "$STATE_FILE" ]; then
    LAST_DATE="$(tr -d '[:space:]' < "$STATE_FILE" || true)"
    if [ -n "$LAST_DATE" ]; then
        SINCE_YMD=""
        if command -v python3 >/dev/null 2>&1; then
            SINCE_YMD="$(python3 -c "import datetime, sys; d=datetime.date.fromisoformat(sys.argv[1])-datetime.timedelta(days=1); print(d.strftime('%Y%m%d'))" "$LAST_DATE" 2>/dev/null || true)"
        fi
        if [ -z "$SINCE_YMD" ]; then
            SINCE_YMD="$(date -d "$LAST_DATE - 1 day" +%Y%m%d 2>/dev/null || date -j -v-1d -f "%Y-%m-%d" "$LAST_DATE" +%Y%m%d 2>/dev/null || true)"
        fi
        if [ -n "$SINCE_YMD" ]; then
            SINCE_ARGS=(--since "$SINCE_YMD")
        fi
    fi
fi

# 5. Execute ccusage
CCUSAGE_ARGS=(ccusage antigravity daily --breakdown --mode calculate --offline --timezone "Asia/Shanghai" --json)
if [ ${#SINCE_ARGS[@]} -gt 0 ]; then
    CCUSAGE_ARGS+=("${SINCE_ARGS[@]}")
fi

RAW_JSON="$("${RUNNER_CMD[@]}" "${CCUSAGE_ARGS[@]}")"
if [ -z "$RAW_JSON" ]; then
    echo "[!] ccusage returned empty output." >&2
    exit 1
fi

HOST_NAME="$(hostname 2>/dev/null || uname -n 2>/dev/null || echo 'unknown-host')"
OS_INFO="$(uname -sr 2>/dev/null || echo 'unix')"

# 6. Wrap payload with hostname and os_info
TMP_PAYLOAD="$(mktemp)"
TMP_GZ="$(mktemp)"
cleanup_tmp() {
    rm -f "$TMP_PAYLOAD" "$TMP_GZ"
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_tmp EXIT

if command -v python3 >/dev/null 2>&1; then
    python3 - "$HOST_NAME" "$OS_INFO" "$RAW_JSON" > "$TMP_PAYLOAD" <<'EOPY'
import json, sys
host, os_info, raw = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    parsed = json.loads(raw)
except Exception:
    start = raw.find("{")
    end = raw.rfind("}")
    parsed = json.loads(raw[start:end+1]) if start != -1 and end != -1 else {"daily": []}
if isinstance(parsed, dict):
    parsed["hostname"] = host
    parsed["os_info"] = os_info
    out = parsed
else:
    out = {"hostname": host, "os_info": os_info, "daily": parsed}
print(json.dumps(out, ensure_ascii=False))
EOPY
else
    printf '{"hostname":"%s","os_info":"%s","data":%s}' "$HOST_NAME" "$OS_INFO" "$RAW_JSON" > "$TMP_PAYLOAD"
fi

gzip -c "$TMP_PAYLOAD" > "$TMP_GZ"

# 7. Upload with 3 retries, 25s timeout; update last_sync_date ONLY on HTTP 2xx
UPLOAD_OK=0
for attempt in 1 2 3; do
    HTTP_CODE="$(curl -sS -o /dev/null -w "%{http_code}" \
        --max-time 25 \
        -X POST "$API_URL" \
        -H "Authorization: Bearer $API_TOKEN" \
        -H "Content-Type: application/json" \
        -H "Content-Encoding: gzip" \
        --data-binary @"$TMP_GZ" || echo "000")"
    if [ "$HTTP_CODE" -ge 200 ] 2>/dev/null && [ "$HTTP_CODE" -lt 300 ] 2>/dev/null; then
        UPLOAD_OK=1
        break
    fi
    sleep $((attempt * 2))
done

if [ "$UPLOAD_OK" -eq 1 ]; then
    TZ="Asia/Shanghai" date +%Y-%m-%d > "$STATE_FILE" 2>/dev/null || date +%Y-%m-%d > "$STATE_FILE"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Sync succeeded (HTTP $HTTP_CODE)."
else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Sync failed after 3 attempts (last HTTP $HTTP_CODE). Watermark preserved." >&2
    exit 1
fi
EOSYNC

# Inject actual endpoint and token into sync.sh
sed -i.bak "s|__BASE_URL__|$BASE_URL|g; s|__API_URL__|$API_URL|g; s|__API_TOKEN__|$API_TOKEN|g" "$SYNC_SCRIPT" && rm -f "$SYNC_SCRIPT.bak"
chmod 700 "$SYNC_SCRIPT"

# Register background scheduler (Linux systemd --user with Persistent=true / macOS launchd)
OS_TYPE="$(uname -s 2>/dev/null || echo 'Linux')"
if [ "$OS_TYPE" = "Darwin" ]; then
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$PLIST_DIR/com.antigravity.usage-hub.agent.plist"
    mkdir -p "$PLIST_DIR"
    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.antigravity.usage-hub.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SYNC_SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/sync.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/sync.log</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST_FILE" >/dev/null 2>&1 || true
    launchctl load -w "$PLIST_FILE" >/dev/null 2>&1 || true
    echo "[+] Registered macOS launchd daemon (every 30m + RunAtLoad)."
else
    if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
        SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_USER_DIR"
        cat > "$SYSTEMD_USER_DIR/ag-usage-hub-agent.service" <<EOF
[Unit]
Description=Antigravity Usage Hub Telemetry Sync Agent
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $SYNC_SCRIPT
EOF
        cat > "$SYSTEMD_USER_DIR/ag-usage-hub-agent.timer" <<EOF
[Unit]
Description=Run Antigravity Usage Hub Sync every 30 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
OnCalendar=*:0/30
Persistent=true

[Install]
WantedBy=timers.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now ag-usage-hub-agent.timer
        echo "[+] Registered systemd --user timer (every 30m, Persistent=true)."
    elif command -v crontab >/dev/null 2>&1; then
        CRON_LINE="*/30 * * * * /bin/bash $SYNC_SCRIPT >> $INSTALL_DIR/sync.log 2>&1"
        (crontab -l 2>/dev/null | grep -v "$SYNC_SCRIPT" || true; echo "$CRON_LINE") | crontab -
        echo "[+] Registered crontab schedule (every 30m)."
    fi
fi

echo "[*] Running initial full history sync now..."
/bin/bash "$SYNC_SCRIPT"
echo "✅ Antigravity Usage Hub probe installed & initial sync verified successfully!"
"""


_PS1_TEMPLATE = """# ==============================================================================
# Antigravity Usage Hub - Client Auto-Installer & Sync Daemon (Windows PowerShell)
# Device: __DISPLAY_NAME__ (__DEVICE_ID__)
# ==============================================================================
$ErrorActionPreference = "Stop"

$BaseUrl = "__BASE_URL__"
$ApiUrl = "__API_URL__"
$ApiToken = "__API_TOKEN__"
$InstallDir = Join-Path $HOME ".ag-usage-hub"
$SyncScriptPath = Join-Path $InstallDir "sync.ps1"

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

$SyncScriptContent = @'
$ErrorActionPreference = "Stop"
$BaseUrl = "__BASE_URL__"
$ApiUrl = "__API_URL__"
$ApiToken = "__API_TOKEN__"
$InstallDir = Join-Path $HOME ".ag-usage-hub"
$StateFile = Join-Path $InstallDir "last_sync_date"

# 1. Enrich PATH for Scheduled Task context
$ExtraPaths = @(
    (Join-Path $HOME ".bun\\bin"),
    (Join-Path $env:APPDATA "npm"),
    (Join-Path $env:ProgramFiles "nodejs"),
    (Join-Path $env:LOCALAPPDATA "Programs\\nodejs")
)
foreach ($p in $ExtraPaths) {
    if ((Test-Path $p) -and ($env:PATH -notlike "*$p*")) {
        $env:PATH = "$p;$env:PATH"
    }
}

# 2. Named Mutex concurrency lock
$MutexName = "Local\\AgUsageHubSyncLock"
$Mutex = New-Object System.Threading.Mutex($false, $MutexName)
$HasHandle = $false
try {
    try {
        $HasHandle = $Mutex.WaitOne(0, $false)
    } catch [System.Threading.AbandonedMutexException] {
        $HasHandle = $true
    }
    if (-not $HasHandle) {
        Write-Host "Another sync instance is currently running. Exiting."
        exit 0
    }

    # 3. Detect bunx or npx --yes
    $Runner = $null
    $RunnerPrefix = @()
    if (Get-Command bunx -ErrorAction SilentlyContinue) {
        $Runner = "bunx"
    } elseif (Get-Command npx -ErrorAction SilentlyContinue) {
        $Runner = "npx"
        $RunnerPrefix = @("--yes")
    } else {
        throw "Neither bunx nor npx was found in PATH."
    }

    # 4. Watermark last_sync_date lookback (1 day prior)
    $CcArgs = @("ccusage", "antigravity", "daily", "--breakdown", "--mode", "calculate", "--offline", "--timezone", "Asia/Shanghai", "--json")
    if (Test-Path $StateFile) {
        $LastSyncRaw = (Get-Content $StateFile -Raw).Trim()
        if ($LastSyncRaw) {
            try {
                $SinceDate = [datetime]::ParseExact($LastSyncRaw, "yyyy-MM-dd", $null).AddDays(-1).ToString("yyyyMMdd")
                $CcArgs += @("--since", $SinceDate)
            } catch {}
        }
    }

    $AllArgs = $RunnerPrefix + $CcArgs
    $RawOutput = & $Runner @AllArgs | Out-String
    if ([string]::IsNullOrWhiteSpace($RawOutput)) {
        throw "ccusage returned empty output."
    }

    $ParsedData = $RawOutput | ConvertFrom-Json
    $PayloadObj = @{
        hostname = [System.Net.Dns]::GetHostName()
        os_info  = [System.Environment]::OSVersion.VersionString
        data     = $ParsedData
    }
    $JsonPayload = $PayloadObj | ConvertTo-Json -Depth 15 -Compress
    $Utf8Bytes = [System.Text.Encoding]::UTF8.GetBytes($JsonPayload)

    # 5. Compress payload with Gzip
    $Ms = New-Object System.IO.MemoryStream
    $Gz = New-Object System.IO.Compression.GZipStream($Ms, [System.IO.Compression.CompressionMode]::Compress)
    $Gz.Write($Utf8Bytes, 0, $Utf8Bytes.Length)
    $Gz.Close()
    $CompressedBytes = $Ms.ToArray()
    $Ms.Close()

    # 6. POST to API_URL with 3 retries, 25s timeout; update watermark ONLY on 2xx
    $Headers = @{
        "Authorization"    = "Bearer $ApiToken"
        "Content-Encoding" = "gzip"
    }
    $Success = $false
    for ($i = 1; $i -le 3; $i++) {
        try {
            $Resp = Invoke-WebRequest -Uri $ApiUrl -Method Post -Headers $Headers -ContentType "application/json" -Body $CompressedBytes -TimeoutSec 25 -UseBasicParsing
            if ($Resp.StatusCode -ge 200 -and $Resp.StatusCode -lt 300) {
                $Success = $true
                break
            }
        } catch {
            if ($i -eq 3) { throw }
            Start-Sleep -Seconds ($i * 2)
        }
    }

    if ($Success) {
        (Get-Date).ToString("yyyy-MM-dd") | Set-Content -Path $StateFile -NoNewline -Encoding UTF8
        Write-Host "[+] Antigravity telemetry synced successfully to $ApiUrl"
    }
} finally {
    if ($HasHandle) {
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}
'@

$SyncScriptContent = $SyncScriptContent.Replace("__BASE_URL__", $BaseUrl).Replace("__API_URL__", $ApiUrl).Replace("__API_TOKEN__", $ApiToken)
Set-Content -Path $SyncScriptPath -Value $SyncScriptContent -Encoding UTF8

# Register Windows Scheduled Task with StartWhenAvailable
$TaskName = "AgUsageHubAgent"
try {
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SyncScriptPath`""
    $TriggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
    $TriggerLogon = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($TriggerRepeat, $TriggerLogon) -Settings $Settings -Force | Out-Null
    Write-Host "[+] Registered Windows Scheduled Task '$TaskName' (every 30m + StartWhenAvailable)."
} catch {
    Write-Warning "Could not register Scheduled Task automatically: $_"
}

Write-Host "[*] Running initial full history sync now..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $SyncScriptPath
Write-Host "✅ Antigravity Usage Hub probe installed & initial sync verified successfully!"
"""


def render_sh_installer(
    base_url: str,
    api_token: str,
    device_id: str = "",
    display_name: str = "",
) -> str:
    clean_base = base_url.strip().rstrip("/")
    api_url = f"{clean_base}/api/report"
    return (
        _SH_TEMPLATE.replace("__DISPLAY_NAME__", display_name)
        .replace("__DEVICE_ID__", device_id)
        .replace("__BASE_URL__", clean_base)
        .replace("__API_URL__", api_url)
        .replace("__API_TOKEN__", api_token)
    )


def render_ps1_installer(
    base_url: str,
    api_token: str,
    device_id: str = "",
    display_name: str = "",
) -> str:
    clean_base = base_url.strip().rstrip("/")
    api_url = f"{clean_base}/api/report"
    return (
        _PS1_TEMPLATE.replace("__DISPLAY_NAME__", display_name)
        .replace("__DEVICE_ID__", device_id)
        .replace("__BASE_URL__", clean_base)
        .replace("__API_URL__", api_url)
        .replace("__API_TOKEN__", api_token)
    )
