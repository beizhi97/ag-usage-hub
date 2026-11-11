# ag-usage-hub 系统规格说明书 (`SPEC.md`)

### 1. 核心工作流与架构设计
- **零隐私采集**：仅统计日期、模型名、各维度 Token 数与折算 API 成本，绝不读取或上传任何代码与对话文本。
- **网页端动态签发客户端（核心交互）**：
  - 摒弃静态配置文件维护客户端列表的做法。服务端首次启动时若未传入 `AG_ADMIN_KEY`，自动生成随机高强度密钥持久化至 `./data/admin.key` 并打印到控制台日志。
  - 网页看板右上角提供 **`+ 新建客户端`** 按钮。输入管理员密钥与设备名称（如“主力台式机”）后，后端动态生成全局唯一 `device_id` 与专属 `api_token`（格式 `ag_tok_<24位随机hex>`）存入 SQLite `devices` 表。
  - 创建成功后，前端弹窗自动根据当前浏览器访问的 `window.location.origin` 拼装出**“发给客户端 Agent 的一句话安装指令”**（支持一键复制，且支持在设备列表随时重看该指令或吊销设备）。
- **服务端动态渲染安装脚本**：
  - 服务端暴露免 Header 鉴权的专属安装脚本端点：`GET /i/{api_token}.sh`（适配 Linux / macOS）与 `GET /i/{api_token}.ps1`（适配 Windows PowerShell）。
  - 请求到达时，服务端校验 `api_token` 在 `devices` 表中有效（无效返回 404），自动提取外部访问 Base URL（优先取查询参数 `?origin=`，其次取 `AG_PUBLIC_URL` 环境变量，再次取 `X-Forwarded-Proto` + `X-Forwarded-Host` / `Host` 头），将真实的 `API_URL` 与 `API_TOKEN` 直接渲染注入到脚本正文中返回。
  - 客户端执行该管道命令后，自动完成：落盘同步脚本 -> 注册系统级守护进程（Linux `systemd --user` 含 `Persistent=true` / macOS `launchd` / Windows Scheduled Task 含 `StartWhenAvailable`） -> 维护本地断点水位线 `last_sync_date` -> **当场触发首次全量历史上报**。

### 2. 仓库目录结构
```text
ag-usage-hub/
├── SPEC.md
├── README.md
├── .gitignore
├── .env.example
├── requirements.txt              # fastapi, uvicorn[standard], pydantic, pytest, httpx
├── Dockerfile
├── docker-compose.yml
├── deploy.sh                     # 服务端一键部署脚本（优先 Docker Compose，无 Docker 自动回退 venv + systemd）
├── server/
│   ├── __init__.py
│   ├── config.py                 # 环境变量与 admin.key 自动生成逻辑
│   ├── db.py                     # SQLite WAL 模式初始化与 CRUD
│   ├── middleware.py             # Content-Encoding: gzip 自动解压中间件（限 10MB）
│   ├── installer_templates.py    # 动态渲染 .sh 与 .ps1 客户端自举安装脚本（内嵌同步脚本与保活注册逻辑）
│   ├── main.py                   # FastAPI 路由（上报、统计、设备管理、动态安装脚本）
│   └── static/
│       └── index.html            # 暗色战绩看板（含“+ 新建客户端”弹窗与一键复制 Agent 指令）
└── tests/
    └── test_server.py            # Pytest 完整验收测试
```

### 3. 数据库 Schema (`server/db.py`)
启用 `PRAGMA journal_mode=WAL;` 与 `PRAGMA busy_timeout=5000;`：
- `devices` 表：
  - `device_id TEXT PRIMARY KEY` (如 `dev_8f3a1b`)
  - `display_name TEXT NOT NULL` (用户在网页填写的名称)
  - `api_token TEXT UNIQUE NOT NULL` (服务端签发的 `ag_tok_...`)
  - `hostname TEXT DEFAULT ''`
  - `os_info TEXT DEFAULT ''`
  - `created_at TEXT NOT NULL`
  - `last_seen_at TEXT DEFAULT ''`
- `daily_records` 表（联合主键 `PRIMARY KEY (device_id, date)`）：
  - `device_id TEXT NOT NULL`, `date TEXT NOT NULL` (`YYYY-MM-DD`)
  - `input_tokens INT`, `output_tokens INT`, `reasoning_tokens INT`, `cache_creation_tokens INT`, `cache_read_tokens INT`, `total_tokens INT`
  - `total_cost REAL`, `model_breakdowns TEXT` (JSON 数组), `updated_at TEXT NOT NULL`

### 4. API 路由规范 (`server/main.py`)
1. **客户端管理接口（需请求头 `X-Admin-Key` 匹配 `AG_ADMIN_KEY`，错返 `401/403`）**：
   - `POST /api/devices`：入参 `{"display_name": "主力工作站", "origin": "https://..."}`。创建设备并返回 `device_id`, `display_name`, `api_token`，以及拼装好的 `agent_prompt`（即给客户端 Agent 的一句话安装指令）。
   - `GET /api/devices/{device_id}/prompt?origin=...`：重新获取某台已有设备的 `agent_prompt` 安装指令（方便重装系统复用）。
   - `DELETE /api/devices/{device_id}?purge_data=false`：吊销客户端 Token（可选是否连带清除历史 `daily_records`）。
2. **动态安装脚本分发接口**：
   - `GET /i/{api_token}.sh` 与 `GET /i/{api_token}.ps1`：校验 `api_token` 合法后返回 `text/plain; charset=utf-8` 脚本。
   - 脚本内嵌逻辑要求：
     - 自动探测 `bunx` 或 `npx --yes`（补全 `~/.bun/bin`、`~/.nvm`、`/opt/homebrew/bin`、`/usr/local/bin` 等 PATH）。
     - 并发防重锁（Linux/macOS 原子锁目录或 `flock`，Windows 命名 Mutex）。
     - **断点水位线补传**：读取本地状态文件 `last_sync_date`。不存在则全量执行 `ccusage antigravity daily --breakdown --mode calculate --offline --timezone "Asia/Shanghai" --json`；存在则将记录日期往前推 1 天作为 `--since YYYYMMDD` 增量拉取。
     - 包装 `hostname` 与 `os_info` 后 POST 至 `{BASE_URL}/api/report`（支持 gzip 压缩，超时 25s，重试 3 次）。**仅当 HTTP 2xx 成功时才更新 `last_sync_date`**，确保关机/断网恢复后 100% 自动补齐漏报天数。
     - 安装时自动注册每 30 分钟运行一次及开机/唤醒补跑的系统级守护任务，并在安装末尾立即同步执行一次上报，打印明确的成功提示。
3. **数据上报接口 `POST /api/report`**：
   - 校验 `Authorization: Bearer <api_token>`，从 `devices` 表反查 `device_id`（不存在返 `403`）。
   - 兼容 `ccusage` 的 `camelCase` 与 `snake_case` 字段（如 `inputTokens`/`input_tokens`、`cacheReadTokens`/`cacheReadInputTokens`、`modelBreakdowns`/`model_breakdowns` 及嵌套 `sections.daily.daily`）。
   - 按 `(device_id, date)` 执行事务级 `UPSERT` 幂等覆盖，同步刷新 `devices.last_seen_at`、`hostname`、`os_info`。
4. **大盘聚合接口 `GET /api/stats?device=all`** 与页面 `GET /`、`GET /health`：
   - 若设置了可选环境变量 `AG_LOCK_VIEW=true`，则访问页面与统计需校验密码；默认看板只读开放，仅新建/删除设备强制校验 `AG_ADMIN_KEY`。
   - 返回总计 KPI、本月订阅回本倍率 ROI（默认按 `AG_SUB_MONTHLY_USD=200` 计算）、设备在线状态（`<=1h` 为 `online`，`1~24h` 为 `idle`，`>24h` 或从未上报为 `offline`）、每日序列以及模型成本排行。

### 5. 前端看板交互 (`server/static/index.html`)
- Tailwind CSS + ECharts 5 深色极客主题。
- **顶部操作栏**：设备筛选器、各设备在线状态呼吸灯胶囊、以及醒目的 **`+ 新建客户端`** 按钮。
- **新建客户端模态框**：
  1. 输入管理员密钥（校验通过后缓存在 `localStorage` 免重复输入）与客户端名称（如 `MacBook-Pro`）。
  2. 点击生成后，高亮展示生成的**“客户端 Agent 一句话安装指令”**与**【一键复制】**按钮，文案格式固定为：
     `请在本机安装 Antigravity Token 监控探针并开启开机保活与断点补传：Linux/macOS 执行 curl -fsSL "{origin}/i/{token}.sh" | bash ；Windows 在 PowerShell 执行 irm "{origin}/i/{token}.ps1" | iex ，完成后验证首次历史数据上报成功。`
- **4 大成就卡片 + 3 大图表**：累计总 Token（自适应 M/B 单位）、等价 API 美金总账单、本月 $200 订阅回本倍率进度条、总缓存命中率；近 180 天 GitHub 风格绿格子编码热力图；每日 Token 与美金双轴趋势图；模型消耗占比环形图与排行榜。

### 6. 服务端一键部署脚本 (`deploy.sh`)
- 支持在全新 Linux 服务器上幂等执行 `bash deploy.sh`：
  - 若不存在 `.env`，自动生成 32 位随机 `AG_ADMIN_KEY` 写入 `.env`。
  - 若检测到 `docker` 与 `docker compose`，自动构建并后台启动容器（默认绑定 `127.0.0.1:8787` 或通过 `AG_BIND` 指定，开启 `--proxy-headers --forwarded-allow-ips=*`）。
  - 若无 Docker，自动使用 `python3 -m venv` 安装依赖并注册/启动 `systemd` 服务 `ag-usage-hub.service`。
  - 轮询 `http://127.0.0.1:8787/health` 确认健康后，在终端清晰输出本地监听端口、Nginx 反代配置片段示例以及当前的 **`AG_ADMIN_KEY`**。

### 7. 自动化验收测试 (`tests/test_server.py`)
必须包含并通过以下 Pytest 用例：
1. 管理员密钥校验、动态新建客户端（`POST /api/devices`）与吊销客户端闭环测试。
2. 动态安装脚本渲染测试：请求 `/i/{token}.sh?origin=https://ag.example.com` 与 `.ps1`，验证返回 200 且脚本内准确注入了 `https://ag.example.com/api/report` 与该设备的 `token`，非法 token 返回 404。
3. 异构 `ccusage` JSON（驼峰/下划线字段）+ Gzip 压缩请求上报测试。
4. 同一设备同日多次上报的幂等覆盖（防累加翻倍）测试，以及多设备汇总（`device=all` vs 单设备过滤）准确性测试。
5. 设备 `online` / `idle` / `offline` 状态机判定测试。
