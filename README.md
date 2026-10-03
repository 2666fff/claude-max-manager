# Claude Max Manager · 账号与额度

Windows 本地桌面工具，用于管理通过官方 Claude Code 登录的多个 Max 账号、查看额度和接续受管任务。Python 标准库实现，无需第三方代理。

![Claude Max Manager 界面，使用虚构账号和示例额度](docs/screenshots/dashboard.png)

> 截图来自真实界面的离线演示：邮箱、额度及时间均为虚构数据，不包含真实账号或凭据。本项目为非官方工具，与 Anthropic 无隶属关系。

## 安装与启动

需要 Windows 10/11、Python 3.10+（含 Tkinter）、官方 Claude Code 和自己的 Max 账号。

```powershell
git clone https://github.com/2666fff/claude-max-manager.git
cd claude-max-manager
claude auth login
python manager.py
```

首次使用前先完成一个账号的官方登录，工具会将其登记为第一个账号。后续账号使用界面中的“＋ 增加账号”。需要隐藏 Python 控制台时，可执行 `pythonw manager.py`。

工具自动查找用户目录下的原生安装、npm 安装或 PATH 中的 `claude.exe`。其他安装位置可指定：

```powershell
$env:CLAUDE_CODE_EXECUTABLE = 'C:\path\to\claude.exe'
python manager.py
```

无需 `pip install`。`Enroll-Accounts.ps1` 是最初的四账号批量授权辅助入口，一般使用桌面界面即可。

## 功能

- **账号管理**：增加账号、官方重新授权、别名、停用自动选择、移除和恢复。重复账号拒绝加入，重授权身份不一致时恢复原配置。
- **额度**：五小时、每周及官方返回的模型额度，已用比例、恢复时间、查询时间。默认每五分钟查询，失败退避；旧数据明确标记，不能参与自动选择。
- **授权状态**：显示授权期限；备用账号接近访问令牌到期时尝试刷新。授权撤销需要重新登录；临时网络错误、403、429不会误报为授权撤销。运行中的默认账号交由官方 CLI 管理刷新。
- **手动切换**：备份并事务更新身份和 OAuth 凭据，保留其他凭据及配置，通过官方 `auth status` 校验；失败回滚。普通 Claude 进程运行中拒绝切换。
- **空闲自动切换**：勾选后，在阈值、周额度、模型额度、数据新鲜度、账号启用状态及冷却期都满足时选择备用账号；普通 CLI 运行中等待其退出。所有账号耗尽时等待恢复。
- **受管任务**：单独启动的任务监控器，在官方 CLI 明确报告额度错误并退出后，选择可用账号，恢复同一会话。普通错误和权限问题停止；正常完成后不再自动提交。只有该方式启动的任务可自动接续。
- **设置与日志**：阈值、查询间隔、切换冷却和额度筛选模型；事件日志不保存令牌或任务内容。

## 使用

1. 点“＋ 增加账号”，在官方网页授权后自动加入列表。
2. 手动切换前退出普通 Claude Code；切换后在原项目执行 `claude --continue`。
3. “空闲时自动换号”默认关闭。开启后关闭窗口会最小化；取消勾选后可退出工具。
4. 需要无人值守接续时，打开“受管任务”，确认项目目录、会话 ID、任务指令和官方权限模式。接续旧任务先退出原 Claude；监控器也会等待其他 Claude 进程退出。运行输出显示在独立终端中，“停止”只中断它自己启动的进程。
5. 授权被撤销时使用该账号卡片上的“重新授权”；额度用完不等于授权失效。

Claude Code 自身的 `/login` 可以在当前进程中重新登录。**本工具尚未接入该进程内换号流程**，而是从外部更新凭据，因此手动切换要求普通 Claude 进程先退出；这是本工具的实现限制，不是 Claude Code 必须重启才能换号。受管任务采用的是限额退出后换号、恢复会话，也不等同于进程内热切换。

本工具不会接管已经运行的普通终端任务，也没有验证过运行中替换凭据的热切换。自动切换不会增加账号额度。没有可用账号时无法继续推理。

## 数据与实现

账号、设置、日志、缓存和备份位于 `%USERPROFILE%\.claude-max-accounts`；默认凭据位于 `%USERPROFILE%\.claude`，身份配置位于 `%USERPROFILE%\.claude.json`。这些目录含敏感凭据，不要上传分享。移除会归档账号，方便恢复；不撤销官方授权。参阅 [私密数据说明](SECURITY.md)。

- `core.py`：账号存储、官方身份校验、事务切换与回滚。
- `enhanced.py`：令牌刷新、互斥锁、刷新结果恢复、额度缓存和调度。
- `manager.py`：桌面入口和官方授权界面。
- `runner.py`：受管任务进程、限额识别、同会话恢复及停止。
- `test_features.py`：隔离测试，使用虚构凭据，不触碰真实账号。

无需 Docker、WSL 或第三方代理；不修改 CC Switch 数据库。当前没有系统托盘或开机自启动。若进程异常退出留下锁目录，工具会报告占用，不会擅自抢占凭据写锁。

## 验证范围

执行 `python -m unittest -v test_features`。自动选择、限频退避、授权错误分类、刷新结果恢复、凭据字段保留、进程保护、移除恢复、真实 CLI 限额事件结构均有隔离测试。

实际完成：五账号官方额度查询；使用隔离凭据执行官方 CLI 切换校验；受管任务等待原进程及响应停止；本机 2.1.288 的真实限额响应采样；桌面界面检查。13 项隔离测试通过。

**跨账号接续实测通过：** 在临时配置中，用一次注入的旧额度数据启动已耗尽的测试账号；官方 CLI 返回真实额度错误；同一个 runner 随后校验并切到可用测试账号，以原会话 ID 恢复，成功返回 `OK`，会话记录落盘。测试使用官方安全模式并禁用工具，临时凭据随后删除；全局身份及凭据的前后哈希一致。它证明额度错误后的恢复路径，不代表复杂任务的每个副作用都能自动重试。

`validate_managed_live.py` 为手工验收脚本，会消耗少量真实额度；须明确指定已耗尽和有额度的账号槽位：

```powershell
python validate_managed_live.py --exhausted-slot 1 --available-slot 2
```

**仍未完成的真实验收：** OAuth 刷新端点实测返回过 403/429，冷却后重试仍是 429，尚未得到真实刷新成功响应。自动刷新代码已接入并有隔离测试；暂不能承诺长期无人值守授权续期。凭据不被错误覆盖，临时限频与授权撤销分开显示。

## 参考

参考项目的协议处理和账号管理思路，结合本机官方 CLI 的实际响应独立实现：

- [claude-swap](https://github.com/realiti4/claude-swap)
- [switch_claude_account](https://github.com/countzero/switch_claude_account)
- [claude-account-switcher](https://github.com/KrzysztofZander/claude-account-switcher)

这些项目的热切换宣称不作为本工具的验证结论。

## 生成脱敏截图

```powershell
python tools/capture_demo.py
```

此命令仅使用临时目录、虚构账号和离线数据渲染真实界面，不读取真实账号目录，不调用额度或授权接口。输出 `docs/screenshots/dashboard.png`。
