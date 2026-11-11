# ag-usage-hub (Antigravity Token 遥测与成就看板)

零隐私采集的多设备 Antigravity / Claude Code (`ccusage`) Token 消耗遥测中心与极客战绩看板。

## 核心特性

- **零隐私采集**：仅聚合日期、模型名、各维度 Token 计数与等价 API 美元成本，绝不读取或上传任何代码与对话内容。
- **网页端动态签发客户端**：无需手写配置文件，在看板右上角点击 `+ 新建客户端`，输入管理员密钥即可生成专属 `api_token` 与给客户端 Agent 的一句话安装指令。
- **跨平台一键自举与断点补传**：服务端动态渲染 `/i/{token}.sh` (Linux/macOS) 与 `/i/{token}.ps1` (Windows)，自动完成守护进程注册（`systemd --user` / `launchd` / Windows Scheduled Task）与 `last_sync_date` 断点续传。
- **暗色战绩看板**：4 大核心 KPI 卡片（含 $200 订阅回本倍率 ROI）、近 180 天 GitHub 风格编码热力图、每日 Token 与成本双轴趋势图、模型成本占比环图与排行榜。

## 快速部署

```bash
git clone <REPO_URL> ag-usage-hub
cd ag-usage-hub
bash deploy.sh
```

## 运行测试

```bash
pytest -v
```
