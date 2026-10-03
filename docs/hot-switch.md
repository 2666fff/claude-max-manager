# 运行中切换：实现与实测边界

## 参考实现实际做了什么

检查 [claude-swap 的 switcher.py](https://github.com/realiti4/claude-swap/blob/main/src/claude_swap/switcher.py) 中 `_perform_switch`、[credentials.py](https://github.com/realiti4/claude-swap/blob/main/src/claude_swap/credentials.py) 中 `_write_active_credentials_file` 和 [claude_locks.py](https://github.com/realiti4/claude-swap/blob/main/src/claude_swap/claude_locks.py)：它没有模拟 `/login`、发送终端按键或重启 Claude。它保存离开账号的凭据，在官方文件锁保护下原子写入目标 OAuth 凭据和 `oauthAccount`，依赖 Claude Code 检测凭据变化并重读。

本机 Claude Code 2.1.288 的文件凭据读取路径也能观察到对 `.credentials.json` 修改时间变化的检查及缓存清除。不过，代码标记本身不构成运行中切换成功的证据，所以随后进行了真实请求测试。

## 本工具的实现

1. 确认 Windows、本机 CLI 版本及已记录的运行中会话版本。当前运行中切换只放行实测过的 2.1.288；其他版本可以退出 CLI 后使用普通切换。
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

`validate_hot_switch.py` 可复现这些路径。`test_features.py` 另覆盖运行中切换的配置保留、三把官方锁、校验失败回滚、未知版本拒绝以及自动调度和冷却。

## 不应扩大解读的部分

- 实测采用官方 CLI 的常驻 `stream-json` 输入，覆盖多轮及一轮内后续推理；未逐一验证所有 IDE 扩展、远程会话或其他 CLI 版本。
- 运行中切换只影响读取这些凭据文件的后续请求。使用独立配置目录或显式环境令牌的会话不会因此自动换号。
- 原终端已因限额停在输入框时，需要用户发出下一条指令；自动提交和故障后接续由受管任务负责。
- 跨账号不会迁移服务端提示缓存，也不会增加订阅额度。
- 自动 OAuth 刷新的真实成功验收仍因服务端 403/429 未闭合；热切换成功不代表长期授权续期已经验证。
