# 架构重构执行进度（Phase 1）

## 目标

在不改变外部使用方式（命令、启动方式、会话语义）的前提下，把可独立模块先迁出 `telegram_bot.py`，建立分层骨架。

## 已完成

1. 建立 `src/claude_telegram_bot/` 包结构（`bootstrap / infrastructure / domain / application`）。
2. 启动配置迁移：
   1. 新增 `bootstrap/settings.py`，负责 `--instance` 解析与 `.env` 加载。
   2. `telegram_bot.py` 改为通过 `RUNTIME_PATHS` 获取 env/state 路径。
3. 持久化能力迁移：
   1. 新增 `infrastructure/persistence/bot_state.py`。
   2. `telegram_bot.py` 的 `_load_state/_save_state` 保持同名包装，不改调用方式。
4. Claude 相关能力迁移：
   1. 新增 `infrastructure/claude/activity.py`（活动提取）。
   2. 新增 `infrastructure/claude/history.py`（历史读取与格式化）。
5. Telegram 消息能力迁移：
   1. 新增 `infrastructure/telegram/messages.py`（消息分片、文件提取、文件发送）。
   2. `telegram_bot.py` 通过同名包装函数保留旧调用签名。
6. SessionRepository 抽离（保持语义不变）：
   1. 新增 `domain/repositories/session_repository.py` 抽象边界。
   2. 新增 `infrastructure/persistence/memory/session_repository.py` 内存实现。
   3. 将原会话全局状态映射挂接到 `SESSION_REPO`，保留原命令调用路径。
7. ClaudeGateway 抽离（保持接口不变）：
   1. 新增 `infrastructure/claude/gateway.py`，承载 SDK 调用主流程。
   2. `telegram_bot.py` 中 `call_claude(...)` 改为同签名包装转调 Gateway。
8. 命令用例层试点（保持命令文案和行为不变）：
   1. 新增 `application/use_cases/session_commands.py`。
   2. `/clear`、`/new`、`/switch` 的“决策与文案拼装”迁移到 use case。
   3. handler 保留鉴权、I/O 和 auto-sync 编排。
9. 命令用例层扩展（保持命令文案和行为不变）：
   1. `/sessions`、`/status`、`/topic` 的状态聚合与文本输出迁移到 use case。
   2. handler 保持轻量：鉴权 + 调用 use case + 发送回复。
10. 命令用例层继续扩展（保持命令文案和行为不变）：
   1. `/cd`、`/session`、`/kill` 的业务编排迁移到 use case。
   2. `/kill` 采用“决策(use case) + interrupt 执行(handler)”模式保持异常文案一致。
11. 命令用例层继续扩展（保持命令文案和行为不变）：
   1. `/delete`、`/sync` 的业务编排迁移到 use case。
   2. `/sync` 保留原异常降级行为（编辑失败时仅发送状态文本）。
12. 命令用例层继续扩展（保持命令文案和行为不变）：
   1. `/history` 的参数解析、会话解析与前置校验迁移到 use case。
   2. 保留 handler 中的历史文件读取与异常捕获，降低迁移风险。
13. 最小回归护栏：
   1. 新增 `scripts/smoke_use_cases.py`，覆盖已迁移命令的关键分支。
   2. 可在无 Telegram/Claude 运行态下快速执行基础兼容检查。
14. 策略层抽离（保持行为不变）：
   1. 新增 `domain/policies/group_auth.py`，承载群聊响应判定逻辑。
   2. `telegram_bot.py` 的 `_is_group_chat` 与 `should_respond_in_group` 改为策略包装转调。
15. AskUser / Upload 可测试化：
   1. 新增 `application/use_cases/ask_user.py`（callback 解析与选项解析）。
   2. 新增 `infrastructure/telegram/uploads.py`（上传文件选择与冲突路径生成）。
   3. `handle_ask_callback`、`handle_file` 接入上述纯函数，行为保持一致。
16. 消息/文件主流程进一步用例化（保持行为不变）：
   1. 新增 `application/use_cases/message_flow.py`（消息路由准备、busy/progress/final log 文案、分片标签拼装）。
   2. 新增 `application/use_cases/file_flow.py`（文件 caption 转发计划与无 caption 回执）。
   3. `handle_message`、`handle_file` 改为薄编排层，核心分支判断与文案拼装下沉至 use case。
17. 回归护栏扩展（覆盖新用例）：
   1. `scripts/smoke_use_cases.py` 增加 `message_flow` 与 `file_flow` 路径断言。
   2. 在系统 Python 与 `py11` 下均执行通过。
18. 路由级集成冒烟能力补充：
   1. `telegram_bot.py` 新增 `build_application()`，将 handler 注册与运行解耦，`main()` 仍保持原启动入口。
   2. 新增 `scripts/smoke_bot_routing.py`，离线校验 command/callback/message handler 注册完整性。
19. Phase 3 首步：SQLite 会话仓储（可选后端）：
   1. 新增 `infrastructure/persistence/sqlite/session_repository.py`，在内存语义上增加 SQLite 持久化能力。
   2. 新增 `SESSION_REPO_BACKEND` / `SESSION_REPO_SQLITE_PATH` 配置；默认仍为 `memory`，可切换 `sqlite`。
   3. `post_shutdown` 增加仓储 `flush()`，用于关闭前持久化 durable state。
20. SQLite 回归护栏补充：
   1. `scripts/smoke_use_cases.py` 增加 SQLite durability 校验（session mapping/cwd/sdk sid/topic name）。
   2. `scripts/smoke_bot_routing.py` 在 `memory/sqlite` 后端下均验证通过。
21. Auth 引导策略统一（保持优先级不变）：
   1. 新增 `bootstrap/auth_state.py`，集中处理 owner/allowed users 的 env+state 解析。
   2. `telegram_bot.py` 改为通过 `resolve_auth_bootstrap_state(...)` 初始化 `OWNER_USER_ID` 与 `ALLOWED_USER_IDS`。
22. 重启恢复验收脚本补齐：
   1. 新增 `scripts/smoke_session_recovery.py`，覆盖 SQLite 的“创建→重启恢复→clear→重启恢复→delete→重启恢复”链路。
   2. 在系统 Python 与 `py11` 下均执行通过。

## 兼容性说明

1. 启动方式未变：`python3 telegram_bot.py [--instance <name>]`。
2. 命令与交互未变：`/new`、`/switch`、`/sessions`、`/history`、`/kill`、文件上传等。
3. 会话行为未变：仍使用同一套内存字典、锁和 session 路由逻辑。
4. `start.sh/stop.sh` 不需要修改。
5. `call_claude` 对上层 handler 的调用方式未变（参数和返回值不变）。
6. `/clear`、`/new`、`/switch` 对用户可见文案与参数用法未变。
7. `/sessions`、`/status`、`/topic` 对用户可见文案与参数用法未变。
8. `/cd`、`/session`、`/kill` 对用户可见文案与参数用法未变。
9. `/delete`、`/sync` 对用户可见文案与参数用法未变。
10. `/history` 对用户可见文案与参数用法未变。
11. group/auth 规则与上传、AskUser 行为对用户可见结果未变。
12. `handle_message` 与 `handle_file` 的用户可见文案、并发锁行为、文件转发行为未变。
13. 运行入口未变：`main()` 仍通过 `run_polling` 启动，`build_application()` 仅用于复用构建流程。
14. 默认后端仍为内存实现；未配置 `SESSION_REPO_BACKEND=sqlite` 时行为与此前完全一致。
15. owner/allowed users 的加载优先级保持不变（`TELEGRAM_OWNER_ID` > `TELEGRAM_ALLOWED_USERS` > state file）。

## 校验结果

1. `py_compile` 通过（主文件 + 新增模块）。
2. 在项目实际解释器 `py11` 下 `import telegram_bot` 成功。
3. Gateway/Repository 抽离后，bot 入口与命令注册逻辑保持不变。
4. 命令用例抽离后，`py11` 解释器下 `import telegram_bot` 通过。
5. `scripts/smoke_use_cases.py` 执行通过（`smoke_use_cases: OK`）。
6. 扩展后的 smoke 脚本覆盖 group/auth、AskUser、upload 关键路径并通过。
7. 进一步扩展后的 smoke 脚本覆盖 message/file flow 新用例并通过（系统 Python + `py11`）。
8. 路由级冒烟脚本通过（`smoke_bot_routing: OK`，`py11`）。
9. SQLite durability smoke 通过（`smoke_use_cases: OK`，系统 Python + `py11`）。
10. `SESSION_REPO_BACKEND=sqlite` 下导入与路由冒烟通过（`import telegram_bot`、`smoke_bot_routing: OK`）。
11. SQLite 重启恢复链路冒烟通过（`smoke_session_recovery: OK`，系统 Python + `py11`）。
12. Auth bootstrap 路径 smoke 通过（包含 env/state 优先级断言）。

## 下一步（建议）

1. 将 `scripts/smoke_use_cases.py` 纳入日常重构检查流程。
2. 在可联网环境执行真实 Telegram API 冒烟（/new、/switch、/sessions、文件上传）。
3. 继续 Phase 3：补 owner/allowed users 持久化后端与 SQLite 一体化（可选，保持默认兼容）。
