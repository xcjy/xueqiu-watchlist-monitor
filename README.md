# 雪球自选股变化监测

此仓库提供 GitHub Actions 云端采集方案：每 5 分钟读取指定雪球用户的自选股，发现新增或移除时通过 Bark 推送。定时任务可能因 GitHub 排队而延迟。

## 部署

请按 [GitHub Actions 部署指南](cloud/GITHUB_ACTIONS.md) 操作。雪球用户 ID、自选分组、Bark 推送地址、Cookie 和状态加密密钥都应通过 GitHub Actions Secrets 配置。不要把密钥、Cookie、真实推送地址或数据库提交到仓库。

第一次成功运行只建立监测基线，不会将已有股票作为新增推送。PC 版 SQLite 数据库不会自动迁移至云端。

## 运行内容

- `.github/workflows/xueqiu-watchlist.yml`：每 5 分钟运行一次，也支持手动运行。
- `cloud/worker.py`：采集、比较自选股快照并推送变化。
- `cloud/state_crypto.py`：加密保存跨次运行所需的 SQLite 状态。
- `tracker.py`：雪球列表接口和快照存储逻辑。

请仅监测你有权访问的自选列表。雪球接口和访问策略可能变化；程序不会尝试绕过验证码、登录限制或访问控制。
