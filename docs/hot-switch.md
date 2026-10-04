# 运行中切换：实现与实测边界

## 参考实现实际做了什么

检查 [claude-swap 的 switcher.py](https://github.com/realiti4/claude-swap/blob/main/src/claude_swap/switcher.py) 中 `_perform_switch`、[credentials.py](https://github.com/realiti4/claude-swap/blob/main/src/claude_swap/credentials.py) 中 `_write_active_credentials_file` 和 [claude_locks.py](https://github.com/realiti4/claude-swap/blob/main/src/claude_swap/claude_locks.py)：它没有模拟 `/login`、发送终端按键或重启 Claude。它保存离开账号的凭据，在官方文件锁保护下原子写入目标 OAuth 凭据和 `oauthAccount`，依赖 Claude Code 检测凭据变化并重读。

本机 Claude Code 2.1.288 的文件凭据读取路径也能观察到对 `.credentials.json` 修改时间变化的检查及缓存清除。不过，代码标记本身不构成运行中切换成功的证据，所以随后进行了真实请求测试。

## 本工具的实现

1. 确认 Windows。CLI 或运行中会话的版本号变化不阻挡换号，不再固定要求 2.1.288；后续步骤仍必须完成令牌检查、官方身份校验及事务回滚。
2. 检查目标令牌，需要时先尝试刷新。
3. 按同一顺序持有工具锁、`.oauth_refresh.lock`、配置目录的 legacy 锁以及身份配置锁；锁存活期间更新心跳，不抢占别人的锁。
4. 重新读取并备份当前身份和最新令牌，只替换账号 OAuth 区块及身份信息，保留 MCP 授权、项目和用户设置。
5. 原子替换文件，通过官方本地 `auth status` 检查身份；失败恢复原文件，持久化事务记录用于处理异常中断。
6. 返回“后续请求生效”，不声称已经改变某个正在传输的请求，也不向终端注入“继续”。

桌面手动切换和阈值自动切换调用同一个后端。受管任务继续使用自己的调度器；桌面端检测其运行状态，避免两套调度同时换号。

## 真实验证（2026-10-03）

环境：Windows、官方 Claude Code 2.1.288。独立临时 `CLAUDE_CONFIG_DIR`、独立测试项目，使用自己的两个已授权 Max 账号。真实凭据和会话不进入此仓库。

| 场景 | 观察结果 |
| --- | --- |
| 可用账号发起请求 | 成功 |
| 保持进程，后端切换到耗尽账号，再发起请求 | 真实 `rate_limit` |
| 保持进程，切回可用账号，再发起请求 | 成功 |
| 一轮任务的 Bash 等待工具调用阶段切换到耗尽账号 | 该轮后续推理返回真实 `rate_limit` |
| 工具阶段切换后，再切回可用账号 | 原进程继续成功 |

各轮 PID 和会话 ID 不变；没有退出、重启或 `--resume`。脚本结束时只关闭自己创建的测试进程，并删除临时凭据。全局身份与凭据文件的前后哈希一致。

`validate_hot_switch.py` 可复现这些路径。当前默认账号使用官方 CLI 最近保存的凭据，避免采用账号归档里的旧令牌。`test_features.py` 另覆盖运行中切换的配置保留、三把官方锁、校验失败回滚、新版运行中会话放行、非 Windows 拒绝以及自动调度和冷却。

## 升级后的验证（2026-10-04）

本机 CLI 和正在运行的会话已升级为 **2.1.289**。旧工具因两处精确版本判断拒绝切换：安装的 CLI 版本和会话记录都只能是 2.1.288。已移除这两处限制，保留 Windows 检查、凭据互斥、官方身份校验及失败回滚。桌面手动切换和 99% 自动换号共用该修正。

以 2.1.289 在独立临时配置中直接连接官方服务，完成 **成功 → 切到耗尽账号后真实 `rate_limit` → 切回可用账号成功**；PID 和会话 ID 全程不变，全局身份及凭据哈希前后相同。生产管理器也自动从已耗尽账号切到有额度的账号，原本运行的 Claude 进程仍在运行。

新增 `--alternate-slot` 模式用于两个令牌有效的账号，不要求备用账号恰好耗尽。只在隔离测试进程中设置临时回环 `ANTHROPIC_BASE_URL`，将请求原样转发到官方 HTTPS API，在内存比较消息请求授权头的 SHA-256 与目标账号凭据，不记录令牌、摘要或请求正文。仅设置 base URL 仍使用订阅登录，见 [Anthropic 官方说明](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways)。该模式实测三轮消息分别采用第一个、第二个、第一个账号的凭据；第二轮收到真实限额响应，最后一轮成功，同 PID 和会话不变。

```powershell
python validate_hot_switch.py --available-slot 1 --alternate-slot 5
python validate_hot_switch.py --available-slot 1 --exhausted-slot 5
```

槽位需按实际账号状态选择。此次另一个已耗尽账号的访问令牌过期，官方续期返回 HTTP 429，未重试；以上成功验证使用令牌仍有效的两个账号，不代表长期续期问题已经解决。

## 不应扩大解读的部分

- 实测版本为 2.1.288 和 2.1.289，采用官方 CLI 的常驻 `stream-json` 输入；2.1.288 另覆盖一轮内后续推理。未逐一验证所有 IDE 扩展、远程会话或其余 CLI 版本。
- 运行中切换只影响读取这些凭据文件的后续请求。使用独立配置目录或显式环境令牌的会话不会因此自动换号。
- 原终端已因限额停在输入框时，需要用户发出下一条指令；自动提交和故障后接续由受管任务负责。
- 跨账号不会迁移服务端提示缓存，也不会增加订阅额度。
- 自动 OAuth 刷新的真实成功验收仍因服务端 403/429 未闭合；热切换成功不代表长期授权续期已经验证。
