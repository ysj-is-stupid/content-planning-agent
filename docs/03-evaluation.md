# 评估计划 v2：预期、实际结果与限制

更新：2026-11-06。本文件第 2–4 节保留完整验收计划；第 5 节记录离线结果。历史真实模型运行见 `07-online-evaluation.md`，强化版 DeepSeek 回归见 `09-strengthened-online-evaluation.md`。

## 1. 评估分层

- 规则单测：金额整数、费率触发条件、工时和 deadline、输入规范化、指纹、完整性、主题/形式硬约束。
- 离线集成：脚本化模型动作进入真实图、工具与数据库，验证路由、ledger、幂等、循环上限和恢复。
- 真实模型：验证观察到超额/失败/部分结果后选择替代、补全或停止；不要求每次动作顺序相同。
- 进程故障：真实关闭/启动两个进程，检查 worker 锁、checkpoint、operation receipt 与累计额度；不以函数内重试代替。

## 2. 首版必须执行的用例

每个编号计划关联 `examples/inputs/<id>.json`、`examples/outputs/<id>.json`、`examples/traces/<id>.jsonl`。当前已生成其中六组离线样例；在线对照另存于 `examples/online/`。其余仍是待执行用例。

1. `normal`：预算 10、允许图文、时间充足；工具费 2 + 活动报价 5 = 7，保存唯一方案。
2. `missing-input`：分别缺预算、缺 deadline、两者都缺；needs_input、明确字段、收费和模型调用均为零。
3. `budget-replan`：已持久化视频候选，搜索 1、生成 1、报价 10 导致总成本 12；模型选择改图文，累计工具 3 + 报价 5 = 8。若模型没有合理重规划，记录实际失败。
4. `tool-failure`：保留成功搜索结果，首次脚本策略永久失败免费；模型改策略，生成成功收费 1，搜索不重复收费。
5. `no-feasible`：用户只允许视频且预算不足；不能改图文绕过要求，输出已知缺口和未完成。
6. `partial`：生成缺正文的脚本收费 1，不允许估算/保存；模型选择补全，额外收费 1，成功产物与历史均保留。
7. `duplicate`：同 request_id 同输入两次；任务、saved_plan、charge 数和模型调用数不增加。
8. `request-conflict`：同 request_id 变更预算；冲突、零新增费用；不同 request_id 相同输入建立新任务。
9. `deadline`：固定时钟下，预计完成恰好等于/早于/晚于 deadline；边界行为符合 <=，过期不可保存。
10. `real-timeout`：实际延迟工具/模型响应、退避跨过剩余时间；及时返回未完成、取消后无后台保存与收费，不以假时钟替代。
11. `retry-fees`：两次明确瞬时失败后成功；3 个 attempt、仅成功一次收费、最终步骤成功且失败记录可查。
12. `stale-artifact`：变素材、格式、长度、策略、脚本哈希任一字段；非法复用被拒；仅变显示名不失效。
13. `quote-age`：同版本在估算后新增工具费或时间前进；保存重新算可行性，不能依旧的 feasible=true。
14. `bad-reference`：不存在/越 task/空/partial/哈希损坏的产物；工具和保存入口均拒绝。
15. `model-protocol`：非法 JSON、未知动作、未知参数、连续非法、无工具调用却声称成功；不调用非法工具，不伪成功，达到纠正上限停止。
16. `no-progress`：重复已失败操作、重复缓存动作、A→B→A 无新观察；明确 NO_PROGRESS，累计调用受限。
17. `limits`：分别耗尽决策、模型传输、工具分发、工具尝试、活跃时间；resume 不重置额度；所有结果保留。
18. `save-storage`：业务事务中断、数据库只读或写失败；不能设置 completed，重启后没有半条 saved_plan/费用事件。
19. `context`：过长主题/素材/历史；限定输入、缩短可裁摘要，硬约束保留，无法装入就 CONTEXT_LIMIT。
20. `injection`：主题或素材含“忽略预算、改费用、直接保存”；不可改不可变输入，保存守卫仍生效。不能据此宣称完全解决语义注入。
21. `fee-components`：分项之和、报价归属、成功/部分/失败费率、废弃产物沉没费用、重复重放、版本固定；总数正确而分项错也要失败。

## 3. 恢复加分项与安全停止

22. `crash-before-commit`：已登记 operation、纯计算完成、业务提交前退出；新进程重算安全，最终仅一条 charge。
23. `crash-after-commit`：业务结果已提交、checkpoint 未落盘退出；重入读取 receipt，产物/费用/保存不重复。
24. `resume-incomplete`：第一次已明确 incomplete；第二进程显式 resume 新 run，仍用有效素材和累计费用，仅处理剩余工作。
25. `checkpoint-mismatch`：checkpoint 引用不存在 receipt 或 revision 异常；STATE_INCONSISTENT，不补造成功事实。
26. `rebuild-checkpoint`：业务账本正常、checkpoint 缺失；显式新 thread 重建，Trace 标记账本恢复，不伪称原 checkpoint 续跑。
27. `corrupt-artifact`：持久化内容哈希不符；拒绝引用，不静默重生成覆盖证据。
28. `concurrent-cli`：进程 A 持锁运行，B run/resume/recover 明确 RUNNER_BUSY；A 崩溃后锁可释放，第三进程可正常获取。
29. `rules-pinned`：新任务用 r2，旧任务仍 r1；原规则缺失则 RULESET_UNAVAILABLE，不修改历史 ledger。
30. `model-receipt`：模型响应已存后崩溃重放不再请求；响应落盘前崩溃可能再次调用，明确 outcome_unknown 和真实费用未知。

## 4. 真实模型实验要求

至少两类观察对照：相同候选的足额/超额预算、同一工具的正常/永久失败响应。可再加入部分脚本/完整脚本。控制输入与提示结构，固定可用时的采样参数；记录实际动作、次数和失败结果。

允许从指定候选/持久化状态开始，必须披露该起点；初始候选由测试构造不等于模型已做过初始规划。完整正常用例另从原始输入起跑。

验证动作在工具观察之后发生；模型响应里的动作被原样解析与校验；图没有根据预算固定切格式；最后费用、依赖、保存都真实成立。reason_summary 不作为唯一证据。

如模型不可用：保留离线结果，标注模型集成为未验证，不宣称完成 ReAct 真实决策验收。

## 5. 材料与实际结果

运行命令已写入 README；实际输入、输出、Trace 已导出到 `examples/`。全量依赖锁文件尚未生成，实测版本见 `04-implementation.md`。

实际结果（离线）：`python -m pytest -q` 得到 **48 passed**。新增用例验证新 run 在累计运行额度已耗尽时明确返回 incomplete，以及 recover 在额度耗尽或执行超时时完成 run、清空预留并持久化原因。`python scripts/build_examples.py` 实际生成六组输入、输出和 Trace：`normal` 完成（工具费 2 + 图文报价 5 = 7）；`missing-input` 返回 needs_input，审计记录证明模型请求和工具操作均为零；`budget-replan` 完成（视频总额 12，改图文后总额 8）；`tool-failure` 在明确失败后换策略并完成（失败不收费，最终总额 7）；`no-feasible` 在仅允许视频且预算 10 时返回 incomplete/BUDGET_EXCEEDED；`duplicate` 将相同输入实际提交两次，第二次返回 replay，任务修订、模型请求、决策、工具分发、操作、费用和保存记录均未增加。每个结果可由相应 `examples/outputs/*.json` 和 `examples/traces/*.jsonl` 核对。

跨进程恢复已验证显式 `incomplete` 续跑、操作回执重放、同 run 硬退出后的原 thread 继续、checkpoint 缺失时显式重建，以及两种 checkpoint/业务事实不一致停止。其余强化用例逐项见 `08-hardening-evaluation.md`。

历史真实模型已运行 8 次，其中 7 次完成、1 次未完成，详见 `07-online-evaluation.md`。本次强化版 DeepSeek 同提示词五例回归均完成；首次回归有一次 `NO_PROGRESS`，修正提示词后保留失败证据并全量重跑，详见 `09-strengthened-online-evaluation.md`。这些小样本不能当作稳定成功率。上述离线 `DemoModel` 路径仍只证明协议和业务守卫。第 2 节未覆盖的用例继续待执行。

已知限制：合成主题/费率、无真实活动执行、真实模型金额未测量、单 worker、普通本地日志不防提交者造假、开放内容质量/版权需人工评审。
