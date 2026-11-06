# 运行与数据说明

更新：2026-11-06。

## 输入与输出

输入采用 JSON，示例见 `examples/inputs/normal.json`。必填 `topic`、非负整数 `budget_units`、带时区的 `campaign_deadline_at`。缺失返回 `needs_input` 且不创建收费任务；非法值返回 `invalid_input`。运行输出包含任务状态、任务 ID、当前方案 ID、已发生工具费和最后动作结果。

`run` 首次提交或按 `request_id` 重放；`resume` 对已明确未完成的 task 建立新 run/thread。硬退出后 `recover` 沿原 thread 的同步 checkpoint 继续；checkpoint 缺失且业务库完整时显式使用 `rebuild-checkpoint`。完成任务重放返回保存时的历史结果，查询时不以当前时间改写既有成功事实。`inspect` 只读取摘要；`export` 从权威业务事件表导出 JSONL。

## 本地数据

业务库与 checkpoint 分开。业务库为费用、产物、报价、模型请求、决策和保存的权威；checkpoint 只承载 LangGraph 进度。两库没有共同事务，操作回执用于处理业务已提交但图尚未保存的重入。恢复前检查 checkpoint 的 task/run、决策引用和 revision；不一致返回 `STATE_INCONSISTENT`，不会自动修补。

文件锁限制同一业务库一次只运行一个 CLI worker；只读查询不持有运行锁。不要将同一数据库用于多机并发服务。`runtime/` 可按需备份，删除会丢失任务历史。

## Trace 阅读

`model_request_started` 记录调用前意图；响应未入库的恢复窗口记录 `model_outcome_unknown`。`model_decision` 包含动作、理由摘要、上下文哈希，以及在线运行可取得的供应商 request ID、模型名和 token 用量；`decision_applied` 包含动作执行结果；`operation_started/finished` 包含工具回执，`tool_attempt_failed` 保留瞬时失败；`plan_saved` 是唯一完成依据。`observed_event_ids` 只允许引用当轮实际提供给模型的事件。离线替身的理由文字不证明真实模型因果；在线对照及原始事件见 `07-online-evaluation.md` 和 `examples/online/`。
