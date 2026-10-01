# 用 GitHub Actions 免费定时监测

此方案不需要一直开着电脑或租云服务器。GitHub 每 5 分钟启动一次短任务，读取雪球自选股、比较上次快照，并在有变化时发送 Bark。定时任务可能因 GitHub 排队而延迟，不保证精确到分钟。

## 重要说明

- GitHub 定时任务最短间隔是 5 分钟，因此不能保持原来的 1 分钟频率。
- GitHub 标准托管运行器在公开仓库免费且不限分钟数。私有仓库每月只有有限免费分钟，5 分钟一次的任务通常会超过免费额度。
- 公开仓库中的代码和配置文件会对所有人可见。不要把 Cookie、Bark 推送地址、SQLite 数据库或真实配置写入仓库。把凭据放在 GitHub Actions Secrets；数据库会加密后存成 Actions artifact，每次运行只保留最新的一份。
- 第一次成功运行会建立监测基线，不会把当前已有股票当作新增推送。PC 上原来的数据库不会自动迁到 GitHub。

## 部署步骤

1. 在 GitHub 新建一个仓库。若希望标准运行器免费不限量，请选择公开仓库。将本项目源码上传到仓库默认分支；`.gitignore` 会排除本地数据库、`.env` 和临时加密文件。确认不要上传任何真实凭据。
2. 打开仓库 **Settings → Secrets and variables → Actions**，添加以下 Repository secrets：

   | Secret 名称 | 内容 |
   | --- | --- |
   | `XUEQIU_USERS` | 每行一个用户，格式 `用户ID\|备注名\|分组ID`，例如 `1234567890\|观察对象\|-5` |
   | `BARK_PUSH_URL` | Bark App 提供的完整推送地址 |
   | `XUEQIU_COOKIE` | 可选；目标自选列表需要登录时才填写有效 Cookie |
   | `STATE_ENCRYPTION_KEY` | 用下面的方法生成的随机加密密钥 |

   多个用户时在 `XUEQIU_USERS` 的 Secret 值里逐行填写，例如：

   ```text
   1234567890|观察对象A|-5
   1234567890|观察对象B|-5
   ```

3. 在本机打开 PowerShell 生成随机密钥：

   ```powershell
   python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

   将输出作为 `STATE_ENCRYPTION_KEY` 的 Secret 值保存。不要提交到仓库，也不要发给别人。若此密钥丢失，已保存的加密数据库无法解密。

4. 打开仓库的 **Actions** 页面，选择 **Xueqiu watchlist monitor**，点击 **Run workflow** 手动运行一次。确认工作流成功后会自动每 5 分钟运行。第一次运行只建立基线。

## 查看状态与停止

- 在 **Actions → Xueqiu watchlist monitor** 查看每轮日志；成功运行日志会显示快照数量、变化数量和推送结果。
- 在同一页面可手动运行。要暂停定时监测，可在工作流菜单中选择禁用；重新启用后恢复。
- 雪球接口可能限频或要求登录。遇到采集失败时检查日志、用户 ID、分组 ID 和可选 Cookie。请仅使用本人有权访问的列表及 Cookie。
