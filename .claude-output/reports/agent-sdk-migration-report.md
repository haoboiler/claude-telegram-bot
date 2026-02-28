# tgcc → Claude Agent SDK 迁移报告

> 日期: 2026-03-01
> 项目: claude-telegram-bot (tgcc)
> 目标: 评估将现有 CLI 子进程方案迁移到 Claude Agent SDK 的可行性、收益与风险

---

## 一、现状分析

### 1.1 项目概况

tgcc 是一个 Telegram Bot，充当 Claude Code CLI 的远程接口。用户通过 Telegram 发送消息，Bot 将其转发给 `claude` CLI，实时追踪活动状态，并返回最终结果。

- **代码量**: 单文件 `telegram_bot.py`，约 1173 行
- **依赖**: `python-telegram-bot>=21.0`, `python-dotenv>=1.0.0`
- **Python**: 3.10+
- **认证**: Claude Code CLI 已认证（Max 订阅 OAuth）

### 1.2 核心功能模块

| 模块 | 行数(约) | 功能 |
|------|----------|------|
| Configuration | 1-80 | 环境变量加载、多实例支持 |
| Session & Lock | 89-260 | 会话管理、asyncio.Lock 并发控制、进程追踪 |
| Stream-JSON 解析 | 262-308 | 解析 CLI 的 stream-json 输出，提取活动描述 |
| `call_claude()` | 314-563 | **核心**：子进程管理、心跳、超时、活动追踪、结果提取 |
| Message splitting | 569-591 | 消息分割（Telegram 4096 字符限制） |
| File auto-send | 594-678 | 从回复中提取文件路径并自动发送 |
| Telegram handlers | 684-1134 | 命令处理（/start, /clear, /new, /switch, /sessions, /status, /cd, /session, /kill）、消息和文件处理 |
| Main | 1140-1173 | 启动 |

### 1.3 现有方案的技术实现

```python
# 当前做法：手动管理 CLI 子进程
proc = await asyncio.create_subprocess_exec(
    "claude", "-p", prompt,
    "--output-format", "stream-json",
    "--dangerously-skip-permissions",
    "--max-turns", str(MAX_TURNS),
    "--session-id", session_id,  # 或 --resume
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=WORK_DIR,
)
# 然后逐行解析 stdout 的 JSON stream...
```

**痛点**：
- 手动解析 stream-json（约 250 行代码）
- 手动管理子进程生命周期（注册、杀死、清理）
- 手动维护 session 状态（`session_initialized` dict）
- 缓冲区管理（需设 1MB limit 防溢出）
- 错误处理复杂（"already in use" 自动重试等）
- 结果提取有 5 级 fallback 优先链

---

## 二、Claude Agent SDK 概述

### 2.1 什么是 Agent SDK

`claude-agent-sdk`（PyPI 包名）是 Anthropic 官方提供的 Python SDK，将 Claude Code 的完整 agent 运行时封装为库。它自动处理 agent loop、工具执行、会话管理和上下文管理。

```bash
pip install claude-agent-sdk
```

> **注意**: 旧包名 `claude-code-sdk` 已废弃（最后版本 0.0.25），需迁移到 `claude-agent-sdk`。

### 2.2 两种编程模型

#### `query()` 函数 — 单次查询

```python
from claude_agent_sdk import query, ClaudeAgentOptions

async for message in query(
    prompt="Fix the bug in auth.py",
    options=ClaudeAgentOptions(
        allowed_tools=["Read", "Edit", "Bash"],
        cwd="/path/to/project",
    ),
):
    # message 是结构化对象，不是 JSON 字符串
    print(message)
```

#### `ClaudeSDKClient` 类 — 多轮对话（推荐用于 tgcc）

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async with ClaudeSDKClient(options=options) as client:
    await client.query("Your prompt here")
    async for msg in client.receive_response():
        print(msg)

    # 后续消息保持上下文
    await client.query("Follow-up question")
    async for msg in client.receive_response():
        print(msg)
```

### 2.3 核心能力

| 能力 | 说明 |
|------|------|
| **会话管理** | 自动创建 session_id，支持 `resume` 和 `fork_session` |
| **权限控制** | `permission_mode`（acceptEdits/bypassPermissions）+ `can_use_tool` 回调 |
| **工具白名单** | `allowed_tools=["Read", "Write", "Bash", ...]` |
| **自定义工具** | `@tool` 装饰器 + `create_sdk_mcp_server()` |
| **Hooks** | `PreToolUse` / `PostToolUse` 等生命周期钩子 |
| **MCP 集成** | 原生支持外部 MCP 服务器 |
| **预算控制** | `max_turns`、`max_budget_usd` |
| **错误类型** | `CLINotFoundError`, `ProcessError`, `CLIConnectionError` 等 |
| **中断支持** | `client.interrupt()` 优雅中断 |

### 2.4 认证方式

| 方式 | 环境变量 | 说明 |
|------|---------|------|
| **Anthropic API Key** | `ANTHROPIC_API_KEY` | 官方推荐，按量付费 |
| **Max 订阅 OAuth** | `CLAUDE_CODE_OAUTH_TOKEN` | 社区方案，走 Max 额度 |
| Amazon Bedrock | `CLAUDE_CODE_USE_BEDROCK=1` | AWS 托管 |
| Google Vertex AI | `CLAUDE_CODE_USE_VERTEX=1` | GCP 托管 |

**Max 订阅使用方法**（来自 [GitHub Issue #559](https://github.com/anthropics/claude-agent-sdk-python/issues/559)）：

```bash
# 获取 OAuth token
claude setup-token

# 设置环境变量，SDK 启动的 CLI 进程会使用此 token
export CLAUDE_CODE_OAUTH_TOKEN=<your-token>
```

> **重要**: 这不是官方文档中的推荐方式，但 Issue #559 已关闭为 COMPLETED，社区确认可用。底层原理：SDK spawn 的 CLI 子进程继承环境变量，CLI 本身支持 OAuth token 认证。

---

## 三、功能对照映射

### 3.1 可直接替代的模块

| tgcc 功能 | 当前实现 | Agent SDK 替代方案 | 改善程度 |
|-----------|---------|-------------------|---------|
| **会话创建** | `uuid4()` + `--session-id` | SDK 自动分配 `session_id`（init 消息中） | ★★★ 无需手动管理 UUID |
| **会话恢复** | `--resume session_id` | `ClaudeAgentOptions(resume=session_id)` | ★★☆ API 更清晰 |
| **会话初始化跟踪** | `session_initialized` dict | SDK 内部处理 | ★★★ 删除约 60 行代码 |
| **子进程管理** | `create_subprocess_exec` + 手动清理 | SDK 内部管理 | ★★★ 删除约 100 行代码 |
| **stream-json 解析** | 逐行 `json.loads` + 事件分类 | 结构化 `Message` 对象迭代 | ★★★ 删除约 150 行代码 |
| **结果提取** | 5 级 fallback 优先链 | SDK 返回最终结果 | ★★★ 删除约 80 行代码 |
| **进程杀死 (/kill)** | `proc.kill()` | `client.interrupt()` | ★★☆ 更优雅 |
| **工作目录** | `cwd=WORK_DIR` 传给子进程 | `ClaudeAgentOptions(cwd=WORK_DIR)` | ★☆☆ 等价 |
| **max-turns** | `--max-turns` CLI 参数 | `ClaudeAgentOptions(max_turns=N)` | ★☆☆ 等价 |
| **权限跳过** | `--dangerously-skip-permissions` | `permission_mode='bypassPermissions'` 或 `'acceptEdits'` | ★★☆ 更精细 |

### 3.2 需保留/重写的模块

| tgcc 功能 | 行数(约) | 迁移说明 |
|-----------|---------|---------|
| **Telegram handlers** | ~450 | 保留，这是 Telegram 层逻辑，与 SDK 无关 |
| **消息分割** | ~25 | 保留，Telegram 消息长度限制不变 |
| **文件上传处理** | ~110 | 保留，Telegram 文件收发逻辑不变 |
| **文件自动发送** | ~80 | 保留，从回复中提取路径并发送的逻辑不变 |
| **@session 路由** | ~30 | 保留，这是 tgcc 特有的多会话路由 |
| **多实例支持** | ~15 | 保留，`--instance` 和 env 加载逻辑不变 |
| **活动追踪展示** | ~50 | **需重写**：基于新的 Message 类型解析活动 |
| **心跳/stall 检测** | ~40 | **可能简化**：SDK 的 streaming 可能内置更好的超时处理 |

### 3.3 新增可用的能力

| 能力 | 价值 | 说明 |
|------|------|------|
| **自定义工具** | 高 | 可以给 Claude 添加 `send_telegram_photo`、`notify_user` 等 Telegram 专用工具 |
| **Hooks** | 中 | `PreToolUse` 可审计/拦截危险操作 |
| **Session fork** | 中 | 从同一上下文分支出多个方向 |
| **预算控制** | 中 | `max_budget_usd` 防止跑飞 |
| **结构化错误** | 低 | 类型化异常替代字符串匹配 |

---

## 四、迁移方案

### 4.1 架构对比

```
当前架构:
┌──────────┐    Telegram API    ┌──────────────┐   subprocess   ┌──────────┐
│ Telegram │ ←───────────────→  │ telegram_bot │ ───────────→   │ claude   │
│  用户    │                    │     .py      │  stream-json   │   CLI    │
└──────────┘                    └──────────────┘   stdout/err   └──────────┘

迁移后架构:
┌──────────┐    Telegram API    ┌──────────────┐   Agent SDK    ┌──────────┐
│ Telegram │ ←───────────────→  │ telegram_bot │ ───────────→   │ claude   │
│  用户    │                    │     .py      │  async iter    │   CLI    │
└──────────┘                    └──────────────┘  (结构化消息)   └──────────┘
```

底层仍然是 CLI 子进程，但 SDK 封装了所有交互细节。

### 4.2 会话管理重构

```python
# 当前：手动维护 6 个 dict
user_active_session: dict[int, str] = {}
user_all_sessions: dict[int, dict[str, str]] = {}
session_initialized: dict[str, bool] = {}
session_locks: dict[str, asyncio.Lock] = {}
session_pending: dict[str, int] = {}
session_processes: dict[str, asyncio.subprocess.Process] = {}

# 迁移后：session_id 由 SDK 返回，删除 session_initialized
# session_processes 替换为 client 引用（用于 interrupt）
user_active_session: dict[int, str] = {}
user_all_sessions: dict[int, dict[str, str]] = {}
session_locks: dict[str, asyncio.Lock] = {}           # 保留
session_pending: dict[str, int] = {}                   # 保留
session_clients: dict[str, ClaudeSDKClient] = {}       # 新增：替代 session_processes
```

### 4.3 核心 call_claude 重写

```python
# 迁移后的 call_claude 伪代码
async def call_claude(prompt: str, session_id: str, is_new: bool,
                      thinking_msg=None, chat=None, session_name=""):
    options = ClaudeAgentOptions(
        cwd=WORK_DIR,
        max_turns=MAX_TURNS,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep",
                        "WebSearch", "WebFetch", "TodoWrite", "Task"],
        permission_mode="bypassPermissions",
    )

    if not is_new:
        options.resume = session_id

    activity_log = []
    response_text = ""
    actual_session_id = session_id

    async for message in query(prompt=prompt, options=options):
        # 1. 捕获 session_id
        if hasattr(message, "subtype") and message.subtype == "init":
            actual_session_id = message.data.get("session_id", session_id)

        # 2. 提取活动状态用于展示
        activity = extract_activity_from_message(message)
        if activity:
            activity_log.append(activity)
            await update_thinking_msg(thinking_msg, activity_log, session_name)

        # 3. 提取最终文本
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    response_text += block.text

    return response_text, activity_log, actual_session_id
```

### 4.4 /kill 命令重写

```python
# 当前：
proc = session_processes.get(sid)
proc.kill()

# 迁移后（使用 ClaudeSDKClient 模式）：
client = session_clients.get(sid)
if client:
    await client.interrupt()
```

### 4.5 迁移步骤

1. **安装 SDK**: `pip install claude-agent-sdk`，更新 `requirements.txt`
2. **替换 call_claude()**: 用 `query()` 或 `ClaudeSDKClient` 替代子进程管理
3. **重写活动追踪**: 基于 SDK 的 Message 类型提取活动信息
4. **简化会话管理**: 删除 `session_initialized` 和 `session_processes`，新增 `session_clients`
5. **重写 /kill**: 使用 `client.interrupt()` 替代 `proc.kill()`
6. **处理认证**: 确保 `CLAUDE_CODE_OAUTH_TOKEN` 环境变量可用
7. **测试**: 验证所有命令、并行会话、文件上传/下载、活动追踪
8. **清理**: 删除不再需要的 stream-json 解析代码

### 4.6 代码量变化预估

| 模块 | 当前行数 | 迁移后 | 变化 |
|------|---------|--------|------|
| Session & Lock | 170 | 100 | -70（删除 initialized/processes） |
| Stream-JSON 解析 | 50 | 0 | -50（全部删除） |
| call_claude() | 250 | 80 | -170（SDK 处理大部分逻辑） |
| 活动追踪展示 | — | 50 | +50（新的 Message 类型解析） |
| Telegram handlers | 450 | 430 | -20（简化部分逻辑） |
| 消息分割 + 文件 | 130 | 130 | 不变 |
| 其他 | 120 | 110 | -10 |
| **总计** | **~1170** | **~900** | **-270（减少约 23%）** |

---

## 五、风险评估

### 5.1 高风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **OAuth token 过期** | Bot 停止工作直到手动刷新 token | 监控 token 有效期；捕获认证错误自动通知用户；编写 token 刷新脚本 |
| **SDK 底层仍是 CLI 子进程** | 如果 CLI 升级有 breaking change，SDK 和直接调用一样受影响 | 锁定 SDK + CLI 版本；测试后再升级 |

### 5.2 中风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **OAuth 方案非官方支持** | 未来版本可能被封堵 | 关注 Issue #559 后续动态；准备 API Key fallback |
| **SDK API 不稳定** | 升级可能需要代码修改 | 锁定版本；关注 changelog |
| **活动追踪信息减少** | SDK 的 Message 对象可能不包含当前 stream-json 暴露的所有工具调用细节 | 先做 PoC 验证 SDK 消息中的信息量是否足够 |
| **Max 订阅并发限制** | 多会话并行时可能被 rate limit | 和当前方案风险一样，不是新增风险 |

### 5.3 低风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **调试难度增加** | SDK 封装了 CLI 交互，排查问题需要理解 SDK 内部 | 保留详细日志；阅读 SDK 源码 |
| **包名变更** | 从 claude-code-sdk 到 claude-agent-sdk | 一次性迁移，影响有限 |
| **品牌限制** | 不能以 "Claude Code" 品牌发布 | 自用场景无影响 |

---

## 六、关键验证项（迁移前 PoC）

在正式迁移之前，建议先做一个最小化 PoC 验证以下关键点：

### 6.1 必须验证

- [ ] **OAuth token 认证**: `CLAUDE_CODE_OAUTH_TOKEN` 能否正常与 SDK 配合使用
- [ ] **会话恢复**: `resume=session_id` 是否正确恢复上下文
- [ ] **活动追踪**: SDK 的 Message 流中能否提取出足够的工具调用信息用于实时展示
- [ ] **中断支持**: `client.interrupt()` 或终止 `query()` 迭代是否能正确停止

### 6.2 建议验证

- [ ] **并行会话**: 多个 `query()` 调用同时运行是否互不干扰
- [ ] **长时间运行**: 超过 5 分钟的任务是否能正常完成
- [ ] **大输出**: 大量文件操作的任务是否有缓冲区问题
- [ ] **错误恢复**: CLI 异常退出时 SDK 是否正确抛出异常

### 6.3 PoC 代码骨架

```python
"""最小化 PoC：验证 Agent SDK 核心功能"""
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions

async def poc():
    session_id = None

    # 1. 验证基本查询 + 捕获 session_id
    print("=== Test 1: Basic query ===")
    async for msg in query(
        prompt="List files in current directory",
        options=ClaudeAgentOptions(
            cwd="/home/gkh/claude_tasks/claude-telegram-bot",
            allowed_tools=["Bash", "Glob"],
            permission_mode="acceptEdits",
            max_turns=5,
        ),
    ):
        print(f"[{type(msg).__name__}] {msg}")
        if hasattr(msg, "subtype") and msg.subtype == "init":
            session_id = msg.data.get("session_id")
            print(f"  -> Captured session_id: {session_id}")

    # 2. 验证会话恢复
    if session_id:
        print(f"\n=== Test 2: Resume session {session_id[:8]}... ===")
        async for msg in query(
            prompt="What files did you just list?",
            options=ClaudeAgentOptions(
                resume=session_id,
                max_turns=3,
            ),
        ):
            print(f"[{type(msg).__name__}] {msg}")

    print("\n=== PoC Complete ===")

asyncio.run(poc())
```

---

## 七、结论与建议

### 迁移价值

| 维度 | 评分 | 说明 |
|------|------|------|
| 代码简洁性 | ★★★★☆ | 删除约 270 行手工管理代码 |
| 可维护性 | ★★★★☆ | 结构化 API 替代 JSON 解析，SDK 跟随 CLI 升级 |
| 功能扩展性 | ★★★★★ | 自定义工具、Hooks、MCP 打开新可能性 |
| 稳定性 | ★★★☆☆ | SDK 仍在演进，但比手工管理子进程更可控 |
| 迁移成本 | ★★★☆☆ | 约 1-2 天工作量，核心是 call_claude 重写 |

### 最终建议

**推荐迁移**。具体路径：

1. **先做 PoC**（第六节的验证项），特别是 OAuth token 和活动追踪信息量
2. **PoC 通过后**，按第 4.5 节步骤逐步迁移
3. **保留 git 分支**，旧版本代码随时可回退
4. **锁定 SDK 版本**，避免自动升级导致 breaking change

---

## 附录

### A. 参考资源

- [Agent SDK 官方文档](https://platform.claude.com/docs/en/agent-sdk/overview)
- [claude-agent-sdk-python (GitHub)](https://github.com/anthropics/claude-agent-sdk-python)
- [claude-agent-sdk (PyPI)](https://pypi.org/project/claude-agent-sdk/)
- [Session Management 文档](https://platform.claude.com/docs/en/agent-sdk/sessions)
- [权限配置文档](https://platform.claude.com/docs/en/agent-sdk/permissions)
- [自定义工具文档](https://platform.claude.com/docs/en/agent-sdk/custom-tools)
- [GitHub Issue #559: Max 订阅支持](https://github.com/anthropics/claude-agent-sdk-python/issues/559)

### B. 已有 Telegram + Agent SDK 实现参考

- [claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) — 功能丰富，支持多用户、项目隔离
- [telegram-claude-bot](https://github.com/Yrzhe/telegram-claude-bot) — 多用户，AI 对话 + 文件管理
- [claudegram](https://github.com/NachoSEO/claudegram) — Telegram 到 Claude Code 的桥接

### C. SDK 错误类型速查

```python
from claude_agent_sdk import (
    ClaudeSDKError,        # 基类
    CLINotFoundError,      # claude CLI 未安装
    CLIConnectionError,    # 连接问题
    ProcessError,          # 进程失败（含 exit_code）
    CLIJSONDecodeError,    # JSON 解析失败
)
```
