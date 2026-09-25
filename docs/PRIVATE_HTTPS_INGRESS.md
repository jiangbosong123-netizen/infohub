# 私网 HTTPS 生产入口（P17f）

## 边界与拓扑

InfoHub 的 Windows 生产入口采用以下固定路径：

```text
Mac / 其他已授权的 tailnet 设备
              │ HTTPS + tailnet ACL
              ▼
Windows 上的 Tailscale Serve（TLS 终止）
              │ HTTP，仅本机回环
              ▼
127.0.0.1:8000 → Docker infohub:8000
```

Docker Compose 把宿主机端口固定为 `127.0.0.1:8000:8000`。因此其他机器不能再通过
`http://100.x.y.z:8000` 访问后端。Tailscale Serve 是唯一的远程入口，并由 tailnet ACL
控制访问。不得启用 Funnel，不得在路由器或 Windows 防火墙中公开 8000 端口。

应用要求生产环境设置 `INFOHUB_PUBLIC_ORIGIN=https://<机器名>.<tailnet>.ts.net`。该值只允许
HTTPS、`*.ts.net` hostname，且不能带端口、路径、用户信息、query 或 fragment。v1 API
会在 Bearer 鉴权前拒绝不匹配的 Host。Host 检查不能代替 TLS；真正的传输边界仍是
localhost-only 发布和 Serve 配置。

Tailscale 官方文档说明 Serve 为 tailnet 内服务提供 HTTPS 反向代理和自动证书；`--bg`
配置可跨 Tailscale 重启和设备重启保持。反向代理目标应使用 `http://127.0.0.1`：

- <https://tailscale.com/docs/features/tailscale-serve>
- <https://tailscale.com/docs/reference/tailscale-cli/serve>
- <https://tailscale.com/docs/reference/examples/serve>

## Windows 首次切换（Windows 开机后执行）

以下步骤必须在 Windows 本机的 PowerShell 中完成。切换前先确认仓库代码已合并并由正常的
部署流程拉取；不要从未合并的开发分支直接覆盖生产。

1. 确认 Tailscale 已登录正确 tailnet，Docker Desktop 正常运行。
2. 在仓库目录运行 `tailscale serve --bg 8000`。记录命令输出中的唯一 HTTPS URL。
3. 运行 `tailscale serve status`，确认代理目标为 `http://127.0.0.1:8000`，并确认没有
   Funnel/public 标识。
4. 在生产 `.env` 中设置 `INFOHUB_PUBLIC_ORIGIN` 为第 2 步的完整 HTTPS origin，不加末尾
   路径或端口。
5. 使用 Windows Server Manager 的正常部署动作，或在仓库目录运行
   `docker compose up -d --build`。迁移必须先成功，web 和 worker 才能启动。
6. 运行 `docker compose ps`，确认 `infohub` 与 `infohub-worker` 都是 healthy。

Serve 可以在后端尚未启动时先配置；这使我们能够先取得 URL、写入 `.env`，再启动要求
`INFOHUB_PUBLIC_ORIGIN` 的生产容器。

## 必须完成的生产验收

只有以下项目全部留下证据，P17f 才能标记为生产验收完成：

- Windows 本机访问 `http://127.0.0.1:8000/api/live` 返回 200。
- Mac 通过 Serve 输出的 `https://...ts.net/api/live` 返回 200，浏览器证书正常。
- HTTPS 响应包含 `Strict-Transport-Security: max-age=31536000`。
- HTTPS 下访问一个已登记但尚未携带 token 的 v1 路径返回结构化 401 和
  `WWW-Authenticate: Bearer`。
- Windows Tailscale IP 的 `http://100.x.y.z:8000` 无法连接。
- `tailscale serve status` 只显示 private Serve，没有 Funnel。
- `/api/ready` 返回 200，证明数据库身份与 worker 心跳均正常。

验收时不要把 token、`.env`、完整凭据或终端历史截图提交到 Git。当前 Windows 已关机，
仓库内的证据只证明静态配置和自动化测试通过，不证明证书或运行时网络已经验收。

## 回滚

若 Serve 切换失败，先运行 `tailscale serve off` 停止新入口，再回滚到上一个经过数据库
兼容性确认的镜像/提交并重新部署。不要通过恢复旧数据库来回滚代码，也不要为了临时访问
而把 8000 重新开放到 `0.0.0.0`。数据、blob 和备份目录继续保留；根据 `/api/ready`、
容器日志和 Serve 状态定位问题后再重试。
