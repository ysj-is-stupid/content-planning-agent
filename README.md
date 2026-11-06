# 内容任务规划 Agent

这是按照 `docs/01-requirements.md` 与 `docs/02-solution.md` 实施的本地首版。输入主题、模拟总预算和带时区的活动截止时间；Agent 检索合成素材、生成结构化脚本、估算活动制作成本，并在全部守卫通过后保存方案。离线测试与 DeepSeek 真实模型对照实验均已运行，强化版回归见 `docs/09-strengthened-online-evaluation.md`。

评审文档入口：需求见 `docs/01-requirements.md`，方案与动态决策见 `docs/02-solution.md`，测试结果见 `docs/03-evaluation.md`，AI 辅助与人工修改见 `docs/04-ai-usage.md`，项目复盘见 `docs/05-retrospective.md`。实现和运行补充材料仍保留在 `docs/04-implementation.md` 与 `docs/05-operations.md`。合成素材和费用/耗时清单位于 `data/`。

实际投入从需求澄清到在线复测和材料整理累计约 6 小时以上，采用区间估算，不记录精确起止时间；尚未完成内容见复盘和各评估文档的限制章节。

## 安装与运行

需要 Python 3.11 以上；本机验证版本为 Python 3.13.9。

```powershell
python -m pip install -e .
python -m pytest -q
content-agent run --input examples/inputs/normal.json --model demo
content-agent run --input examples/inputs/budget-replan.json --model demo
content-agent run --input examples/inputs/tool-failure.json --model demo --fail-rich-generation
```

默认运行数据保存在 `runtime/business.sqlite`，LangGraph checkpoint 在 `runtime/checkpoints.sqlite`。两者是本地运行产物，未纳入提交。相同 `request_id` 和相同输入再次提交会返回原任务；修改预算或截止时间须使用新 `request_id`。

```powershell
content-agent inspect --task-id TASK_ID
content-agent resume --task-id TASK_ID --model demo
content-agent recover --task-id TASK_ID --model demo
content-agent rebuild-checkpoint --task-id TASK_ID --model demo
content-agent export --task-id TASK_ID --output trace.jsonl
```

`resume` 针对已明确返回 `incomplete` 的任务建立新 run/thread。硬进程退出后可用 `recover` 沿原 thread 继续；只有 checkpoint 缺失且业务库完整时才显式使用 `rebuild-checkpoint` 建新 thread。业务账本与有效产物会保留。运行命令用全局 OS 文件锁限制为单个 worker。新任务可用全局参数 `--pricing-rules-version r2` 选择 r2 定价；旧任务仍使用创建时的版本。

## 最终交付结构

`run`、`resume`、`recover`、`rebuild-checkpoint`、`inspect` 以及相同 `request_id` 的重复提交都会返回 `delivery`。其中 `task_plan` 含当前或已保存方案、完整脚本和报价引用；`steps` 给出检索、选案、脚本、估价、保存五步的实际状态；`tool_results` 保留每次工具参数、结果、尝试、费用及关联模型决策；`cost_summary` 给出工具费、制作基础费、授权费、合计和预算余额；`saved_plan` 或 `incomplete_reason` 明确最终结果。失败与被替代的工具结果不会因为后续成功而删除。

## 模型模式

- `--model demo`：确定性的离线动作替身，用于测试业务流程，不能作为真实模型自主决策的证据。
- `--model script --script actions.json`：按给定动作序列测试动作协议和错误边界。
- `--model deepseek`：从项目 `.env` 或系统环境读取 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`；已用 `deepseek-chat` 完成在线验证。密钥文件已加入 `.gitignore`。
- `--model openai --model-name gpt-4.1-mini`：使用 `OPENAI_API_KEY`；该路径尚未运行实际模型测试。

```powershell
content-agent --db runtime/online-business.sqlite --checkpoint runtime/online-checkpoints.sqlite run --input examples/inputs/normal.json --model deepseek
python scripts/run_online_eval.py --output-dir examples/online/new-run normal-full budget-low budget-high failure-permanent failure-success
```

第二条命令会消耗 DeepSeek API 用量并创建新的任务；`normal-full` 从原始输入起跑，其余四例从披露的预置业务状态起跑。输入、预置说明、输出和 Trace 写入指定目录，避免覆盖历史证据。

模型只输出一个 JSON 业务动作。LangGraph 负责路由，SQLite 业务库校验材料、脚本、报价、累计工具费、预算和保存时刻的截止时间。工具失败或方案不可行会返回模型决策节点。`generate_script` 使用本地版本化模板，不进行隐藏的第二次模型调用。

## 合成规则 r1

- 检索成功（含空结果）收费 1；脚本完整或部分成功收费 1；明确失败、估算和保存收费 0。
- 视频制作报价为基础 8 + 授权 2，预计 120 分钟；图文为基础 4 + 授权 1，预计 45 分钟。数量倍乘制作报价与工时。
- 总预算校验为已结算工具费 + 当前活动制作报价。被替代方案的工具费用不退；活动报价本身不扣款。
- 保存时重新计算 `当前 UTC 时间 + 制作工时 <= 截止时间`。仅完整、非空且内容哈希有效的脚本可估算和保存。

支持的合成主题：`新品发布` / `产品发布`、`环保活动` / `绿色生活`、`社区活动` / `社区市集`，以及目录中列出的英文别名。素材均标记 `synthetic-demo-only`，不代表真实版权审核。

## 实际证据与限制

运行 `python scripts/build_examples.py` 可重新生成 `examples/outputs` 与 `examples/traces`。这些文件是离线替身的实际执行记录，覆盖正常完成、缺输入、预算 12→8 重规划、脚本工具失败后换策略、仅允许视频时不可行和重复提交。缺输入 Trace 明确记录零模型/零工具调用；重复提交实际运行两次，并核对第二次没有新增任务修订、模型请求、工具操作、费用或保存记录。当前 `python -m pytest -q` 为 **48 passed**；缺口补验见 `docs/08-hardening-evaluation.md`。

历史真实模型 Trace 见 `docs/07-online-evaluation.md`。强化版 DeepSeek 五例回归及一次失败后的提示词修正见 `docs/09-strengthened-online-evaluation.md`。上下文预检包含系统提示词与输出配额，但仍采用保守字节上界；外部付费工具的恰好一次对账、跨版本目录/费用策略/graph schema 迁移及开放内容质量/版权审查仍未完成。
