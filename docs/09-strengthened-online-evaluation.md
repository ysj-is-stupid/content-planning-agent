# 强化版 DeepSeek 在线回归

更新：2026-11-06。只调用 `deepseek-chat`，通过 DeepSeek 兼容接口发出请求；没有调用原生 OpenAI API。运行输入、预置说明、CLI 输出和逐事件 Trace 均保留在 `examples/online/`。

## 执行与证据

离线门槛 `python -m pytest -q` 为 **48 passed**，原始输出见 `examples/test-results.txt`。六组离线样例也重新由 `python scripts/build_examples.py` 生成。

首轮强化版在线运行使用 `examples/online/strengthened-20261106/`，结果为 4 例完成、1 例 `incomplete/NO_PROGRESS`。失败例中模型在 `rich` 脚本策略永久失败后反复改变 `mode`，没有切换 `generator_strategy`。完整失败记录保留在该目录的 `outputs/failure-permanent.json` 与 `traces/failure-permanent.jsonl`。

随后在系统提示词中明确：遇到 `SCRIPT_GENERATION_FAILED` 时，把方案策略从 `rich` 改为 `compact`，并且没有部分产物 ID 时不要调用 `complete_partial`。单例复测写入 `examples/online/strengthened-followup-20261106/`，完成。再用同一版提示词完整重跑五例，最终证据位于 `examples/online/strengthened-final-20261106/`。最终五例均完成；其汇总文件为 `summary-normal-full-budget-low-budget-high-failure-permanent-failure-success.json`。这些目录按时间保留，未覆盖失败证据。

## 最终五例

- `normal-full`：从原始输入起跑，无预置业务步骤。模型依次执行检索、建方案、生成脚本、绑定脚本、估算、保存；6 次决策，工具费 2，总额 7，完成。
- `budget-low`：预置视频候选、完整脚本与报价，预算 10。模型先改图文，再生成、绑定、估算和保存；5 次决策，累计工具费 3，总额 8，完成。
- `budget-high`：相同类型的预置视频候选，预算 20。模型直接保存；1 次决策，工具费 2，总额 12，完成。
- `failure-permanent`：预置 `rich` 脚本生成的永久失败回执。模型首先把方案策略改为 `compact`，随后生成、绑定、估算、保存；5 次决策，工具费 2，总额 7，完成。先前失败的脚本调用未收费。
- `failure-success`：预置成功脚本产物。模型绑定、估算、保存；3 次决策，工具费 2，总额 7，完成。

最终五例共 20 次模型决策。Trace 中每条模型决策的 provider 均为 `deepseek`；供应商回传的 `total_tokens` 合计 46,822。这里仅统计 API 回传 token 用量，未换算货币费用。`setups/` 披露了四个预置起点，所以这些用例不能算从原始输入完成初始规划。

## 范围与限制

在线样本很小，且首轮失败后调整过提示词；最终 5/5 仅说明这五个固定场景在最终版本下完成，不能推断稳定成功率。离线故障注入证明本地 SQLite 与合成工具边界，未证明外部供应商侧副作用恰好一次。DeepSeek 精确 tokenizer、完整跨版本迁移、真实内容质量和版权审核仍未验证。
