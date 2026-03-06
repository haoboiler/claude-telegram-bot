# TGCC 长期架构规划与拆分落地方案

## 1. 背景与问题定义

当前项目的核心逻辑集中在 `telegram_bot.py` 单文件中，带来以下长期风险：

1. 可维护性差：会话管理、权限、Claude 调用、Telegram 适配、文件处理高度耦合。
2. 可测试性差：大量全局状态与副作用，难以做稳定单元测试。
3. 变更风险高：新增一个命令或策略，容易影响不相关路径。
4. 可观测性弱：缺少统一的请求级指标（时延、steps、tokens、成本）。
5. 扩展成本高：后续要引入配额、审计、持久化、后台任务会非常痛苦。

目标是把项目升级为一个可长期演进的“模块化单体”架构，在不牺牲当前可用性的前提下逐步重构。

---

## 2. 目标架构（模块化单体）

## 2.1 架构原则

1. 先分层、后分布式：不直接微服务化，优先降低耦合和认知负担。
2. 领域规则内聚：会话/权限/配额规则进入 domain 层，不散落在 handler。
3. 依赖方向单向：`interfaces -> application -> domain`，`infrastructure` 通过接口反向注入。
4. 行为不变优先：重构阶段先保持功能等价，再做优化。
5. 可观测先行：每次请求必须可追踪、可统计、可审计。

## 2.2 目录蓝图

```text
src/
  bootstrap/
    app.py                    # 程序入口、依赖注入、handler 注册
    settings.py               # 环境变量与配置模型
    logging.py                # 统一日志格式与上下文

  interfaces/
    telegram/
      router.py               # 命令与消息路由
      handlers/
        start.py
        message.py
        session_commands.py
        file_upload.py
        ask_user_callback.py
      presenters/
        message_formatter.py   # Telegram 输出格式化与分片

  application/
    use_cases/
      send_message.py
      process_file.py
      manage_session.py
      manage_topic.py
      interrupt_session.py
      sync_memory.py
      read_history.py
    dto/
    services/
      activity_tracker.py      # 活动日志聚合、节流更新
      file_dispatcher.py       # 响应中的文件自动发送策略

  domain/
    models/
      session.py               # Session/TopicSession/SessionStatus
      auth.py                  # Owner/AllowedUser 策略模型
    policies/
      group_response_policy.py
      quota_policy.py          # 后续扩展：预算/配额策略
    repositories/
      session_repo.py          # 抽象接口
      auth_repo.py             # 抽象接口

  infrastructure/
    claude/
      sdk_client.py            # Claude Agent SDK 适配器
      ask_user_bridge.py       # AskUserQuestion 与 Telegram 桥接
    persistence/
      memory/
        session_repo.py        # 现有行为兼容的内存实现
      sqlite/
        session_repo.py        # 后续持久化实现
    telemetry/
      metrics.py               # 请求计数、耗时、steps、tokens、cost
      audit_log.py             # 安全审计/关键操作日志

tests/
  unit/
  integration/
```

## 2.3 核心边界职责

1. Telegram 层只做输入输出与协议适配，不承载业务规则。
2. Application 层负责编排流程（鉴权 -> 会话路由 -> Claude 调用 -> 输出）。
3. Domain 层负责“什么是合法行为”（会话状态机、群聊响应策略、配额规则）。
4. Infrastructure 层负责“怎么落地”（SDK、存储、日志、外部脚本）。

---

## 3. 为什么这么设计

## 3.1 从“函数堆叠”转为“职责分工”

当前单文件的问题本质不是代码行数，而是“职责交叉”。分层后：

1. 新增命令只改接口和 use case，不碰核心域规则。
2. 修改会话策略只改 domain/policy，不影响 Telegram 协议层。
3. 替换存储（内存 -> SQLite/Redis）不影响业务流程。

## 3.2 为稳定性和成本治理预留位置

你关心的“TGCC 用量偏快”问题，需要在架构上有固定挂点：

1. `quota_policy.py`：限制每次最大 turns、steps、长会话触发 compact/summarize。
2. `metrics.py`：记录每请求 token/cost/step，支持按 session/topic 用户维度分析。
3. `activity_tracker.py`：统一节流和状态展示，防止重复/冗余操作。

## 3.3 面向长期团队协作

模块化后可以并行开发：

1. 一人维护 Telegram 命令与表现层。
2. 一人优化 Claude SDK 调用和成本策略。
3. 一人做持久化和运维能力（实例状态、恢复、审计）。

---

## 4. 拆分实施计划（从哪里开始）

## Phase 0：基线与护栏（1-2 天）

目标：先固定当前行为，避免重构引入回归。

1. 建立 `src/` 骨架与最小 `bootstrap/app.py`（仍调用旧逻辑）。
2. 补一组“行为快照测试”或集成脚本：
   1. `/start`、`/new`、`/switch`、`/sessions`
   2. 普通消息、`@session` 路由
   3. `/kill`、`/history`
   4. 文件上传 + caption 转发
3. 记录当前关键配置默认值和已知差异（README/.env.example/代码）。

验收标准：

1. 启动路径不变，已有实例可继续跑。
2. 核心命令行为和当前版本一致。

## Phase 1：抽离“配置+状态+Claude Gateway”（3-5 天）

目标：先切最核心的三块，降低主文件复杂度。

1. 提取 `settings.py`：环境变量、默认值、配置校验。
2. 提取 `SessionRepository` 接口 + `InMemorySessionRepository` 实现。
3. 提取 `ClaudeGateway`：
   1. 封装 `call_claude`
   2. 封装 AskUserQuestion bridge
   3. 暴露统一返回结构（result/activity/usage/error）
4. `telegram_bot.py` 改为薄入口，调用 application use case。

验收标准：

1. 主文件代码量明显下降（建议 < 1200 行）。
2. 会话并发锁与 `/kill` 行为不变。
3. AskUserQuestion 交互不回退。

## Phase 2：命令与用例分离（4-6 天）

目标：每个命令一个 handler，业务逻辑进入 use case。

1. 拆 `handlers/`：`session_commands.py`、`message.py`、`file_upload.py` 等。
2. 建立 `application/use_cases`：
   1. `SendMessageUseCase`
   2. `ProcessFileUseCase`
   3. `ManageSessionUseCase`
3. 把 group 策略与授权判断沉到 domain policy。

验收标准：

1. Telegram handler 只做参数解析与返回消息。
2. 命令新增不再需要改大段共享全局代码。

## Phase 3：持久化与恢复能力（3-5 天）

目标：解决重启丢状态和多实例一致性问题。

1. 引入 SQLite repo（会话、topic_active、sdk_session_id、cwd 映射）。
2. 启动时恢复状态，进程退出前 flush。
3. 对 owner/allowed users 统一持久化策略，移除 YAML/JSON 双轨歧义。

验收标准：

1. Bot 重启后 session 可恢复。
2. 不依赖全局 dict 才能工作。

## Phase 4：成本治理与可观测（2-4 天）

目标：把“用量快”的问题产品化治理。

1. 记录每请求：topic/session/user、turns、steps、duration、tokens、cost。
2. 增加策略：
   1. 默认 `max_turns` 下调（如 30）
   2. 超 step 提示继续确认
   3. 长会话自动建议 compact/summarize to new session
3. 增加 `/usage`、`/budget` 命令（可选）。

验收标准：

1. 能看见每个 session 的资源消耗。
2. 用户可配置预算或上限策略。

---

## 5. 第一周开工清单（可直接执行）

1. 建立 `src/bootstrap/settings.py`，迁移所有 env 读取。
2. 新建 `src/domain/repositories/session_repo.py` 接口。
3. 新建 `src/infrastructure/persistence/memory/session_repo.py`，搬迁当前全局会话字典能力。
4. 新建 `src/infrastructure/claude/sdk_client.py`，迁移 `call_claude` 及相关活动提取函数。
5. `telegram_bot.py` 保留兼容入口，只做：
   1. 初始化依赖
   2. 注册 handler
   3. 调用 use case

交付物：

1. 可运行代码（行为等价）。
2. 一份模块依赖图（Mermaid）。
3. 一份重构迁移日志（每阶段变更与风险记录）。

---

## 6. 风险与回滚策略

1. 风险：重构初期并发锁语义变化，导致队列行为偏差。  
   回滚：保持旧 `telegram_bot.py` 入口可切换（feature flag）。
2. 风险：状态迁移到 SQLite 时数据丢失。  
   回滚：迁移前导出内存快照，保留旧状态文件读取逻辑一版。
3. 风险：AskUserQuestion 回调链断裂。  
   回滚：先保留旧 callback 实现，网关切换采用灰度。

---

## 7. 里程碑定义

1. M1（结构化完成）：主文件降至“薄入口”，核心逻辑迁出。
2. M2（可恢复完成）：重启后会话连续、状态一致。
3. M3（治理完成）：可见、可控、可优化的 token/cost 使用体系。

达到 M3 后，这个项目就从“脚本型 bot”升级为“可持续演进的产品级代理系统”。
