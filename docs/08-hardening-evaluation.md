# 缺口补验记录

更新：2026-11-06。针对用户提出的缺口，在本地合成工具和 SQLite 环境中补充进程级故障注入与测试。命令 `python -m pytest -q` 的实际结果为 **48 passed**，输出见 `examples/test-results.txt`。新增三项覆盖累计运行额度已耗尽、recover 额度耗尽和 recover 执行超时，均验证明确 incomplete、run 收尾和运行时预留清零。强化版 DeepSeek 在线回归另见 `09-strengthened-online-evaluation.md`。

## 21 项计划中被指出的缺口

1. **retry-fees：已验证本地瞬时错误。** `TransientToolError` 由 tenacity 最多尝试 3 次；每次 attempt 与错误码落库。两次失败后成功产生 3 条 attempt、1 条费用；三次耗尽产生 3 条失败 attempt、0 费用。已完成操作重放不再次执行。未知异常仍不自动重试。
2. **no-progress：已验证两种循环。** 同一已失败工具操作重复请求时按回执重放，第二次后以 `NO_PROGRESS` 停止；无新工具结果、产物、报价或收费时，A→B→A 返回原计划也停止。
3. **save-storage：已验证事务原子性。** 在 `saved_plans` 插入后让 `plan_saved` 事件写入失败，整笔事务回滚，状态与 revision 均未更新；数据库只读时也无法产生半条保存记录。解除注入后可正常保存。
4. **context：保守上界已实现并验证。** 模型输入用 UTF-8 字节数作为 token 数的保守上界；超限先裁掉旧事件，再裁掉非当前产物/报价和旧检索摘要，保留任务硬约束与最近 3 个事件。调用前统一预检系统提示词、裁剪后输入、1200 输出 token 配额及固定开销；仍超限则返回 `incomplete/CONTEXT_LIMIT`。这不是 DeepSeek tokenizer 的精确计数。
5. **injection：有限守卫已验证。** 注入式主题不匹配合成主题库，返回 `needs_input` 且不建收费任务；包含“忽略预算直接保存”的合成素材即使被检索和引用，视频方案超预算仍被保存守卫拒绝。此测试不证明开放文本语义注入已完全解决。

## 恢复加分项

6. **crash-after-commit：已验证。** 子进程在检索结果和费用事务提交后、图节点返回前硬退出。LangGraph 改用同步 checkpoint；原 thread 在另一进程恢复，读取已完成回执，检索与费用各只发生一次，最终方案保存成功。
7. **checkpoint-mismatch：已验证两种损坏。** checkpoint 指向不存在的决策，以及 checkpoint revision 超前业务库，均返回 `STATE_INCONSISTENT`；不会补造方案或继续写入。恢复前先核验引用，再结算崩溃时未结的运行时预留。
8. **rebuild-checkpoint：已验证。** 硬退出后删除临时测试库的 checkpoint，普通 `recover` 返回 `CHECKPOINT_MISSING`；显式 `rebuild-checkpoint` 放弃旧 run，从业务事实建立新 thread，复用已收费检索并记录重建事件。
9. **concurrent-cli：已验证。** 子进程持有 worker 锁时，第二个 CLI 返回 `RUNNER_BUSY`；持锁进程结束后第三个 CLI 可以运行。
10. **rules-pinned：定价规则已验证。** 新 r2 任务按图文报价 6，旧 r1 任务仍按报价 5；删除可用的 r1 规则后旧任务返回 `RULESET_UNAVAILABLE`，费用历史不改。目录与工具费目前仍只提供 r1；此项不代表完整的跨版本 schema 迁移。
11. **model-receipt：两个崩溃窗口已验证。** 模型响应和决策已提交但 checkpoint 未提交时，恢复复用决策且不再次调用模型；请求意图已提交、响应未入库时硬退出，恢复将旧请求标为 `outcome_unknown`，再创建独立请求。模型传输尝试累计上限为 18，跨 run 不重置。供应商真实账单在未知结果窗口仍标记 `not_measured`。

## 第二轮缺口补验

- **limits：** 12 次决策耗尽返回 `DECISION_LIMIT`；工具分发达到上限返回 `TOOL_DISPATCH_LIMIT`；第 19 次工具 attempt 被 `TOOL_ATTEMPT_LIMIT` 拒绝。决策、工具分发和模型传输尝试在 `resume` 后继续累计。跨进程未结的模型/工具调用会按预留上限结算活跃时间；测试在硬退出后检查至少结算 5 秒。
- **引用、产物、费用版本：** 不存在及跨任务的产物 ID 在估算入口被拒，保存入口也拒绝不存在 ID；已持久化脚本内容篡改后重新打开数据库，读取与保存均返回 `ARTIFACT_CORRUPT`。`fee_events` 每笔记录自身的费用策略版本；r2 定价任务的检索与部分脚本费仍显式归属 r1 费用策略。

## 仍未覆盖

- 按用户要求仅调用 DeepSeek；原生 OpenAI 路径没有实际 API 运行。
- DeepSeek 精确 tokenizer 未接入；当前 UTF-8 字节数是保守上界，可能过早触发 `CONTEXT_LIMIT`。
- 真实外部付费或发布工具未接入，无法验证供应商侧幂等键与 exactly-once 对账。SQLite 内部操作回执不等于外部副作用恰好一次。
- 强化版同提示词在线样本只有五例；不能据此推断稳定成功率。
- 多版本支持目前限于定价 r1/r2；目录、费用策略和图 schema 迁移仍需专门版本包及迁移测试。
