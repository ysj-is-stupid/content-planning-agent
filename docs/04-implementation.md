# 实施记录：首版与强化验证

更新：2026-11-06。实施顺序遵循 `02-solution.md` 第 14 节。

## 已完成

1. `schemas.py`、`canonical.py`、`rules.py`：严格输入与方案结构，UTC 时间归一、规范化指纹、模拟费率 r1、制作报价、完整脚本资格和保存可行性纯函数。缺输入、预算分项、12→8、deadline 边界、部分/损坏/旧规格脚本均有单测。
2. `repository.py`、`catalog.py`：合成素材库、SQLite 业务库、request_id 唯一约束、不可变计划版本、操作意图与回执、attempt 历史、唯一收费记录、保存事务。工具纯计算的未结算操作可在重启后重算；已完成操作只读取回执。
3. `runner.py`、`cli.py`：LangGraph 异步最小图、SQLite checkpoint、模型单动作协议、离线替身与 OpenAI 适配入口、12 次决策上限、工具操作/attempt 上限、活跃运行时间上限、重复错误与连续非法动作停止。`resume` 在新进程创建新 run/thread，保留业务账本和可用结果。
   当前图由 `reconcile`、`decide`、`apply` 三个节点组成；动作校验、计划变更和工具 dispatch 合并在 `apply` 内，业务事实仍由 Repository 事务持久化。
4. `examples`：五个离线场景的实际输入、输出和数据库事件导出。所有记录均由运行代码生成，不是手写模拟 Trace。
5. 强化阶段：tenacity 三次瞬时重试、attempt 独立留痕、无进展停止、包含系统提示词与输出配额的保守上下文预检、同步 checkpoint、同 run 恢复、缺失 checkpoint 显式重建、模型请求意图与未知结果记录、跨 run 决策/模型传输/工具分发上限、硬退出运行时预留结算、run/recover 额度与超时统一未完成收尾、worker 进程锁与定价 r1/r2 固定。故障注入结果见 `08-hardening-evaluation.md`。

## 本机验证版本

- Python 3.13.9；Pydantic 2.12.4；pytest 8.4.2。
- LangGraph 1.2.20；langgraph-checkpoint-sqlite 3.1.3；aiosqlite 0.22.1。
- portalocker 3.2.0；tenacity 9.1.2；OpenAI Python SDK 2.54.0。
- python-dotenv 1.1.0；DeepSeek `deepseek-chat` 经 OpenAI SDK 兼容接口在线验证。

`pyproject.toml` 约束直接依赖的主版本；尚未生成包含传递依赖的跨平台锁文件。以上为实际测试环境版本，不应理解成任意环境已验证。

## 尚未完成或验证

- DeepSeek 强化版五例同提示词在线回归已运行；历史与本次失败记录分别见 `07-online-evaluation.md` 和 `09-strengthened-online-evaluation.md`。按用户要求未运行原生 OpenAI API。
- 硬进程崩溃后的未结运行时预留在通过 checkpoint 一致性检查后按全额结算；正常调用仍按实际耗时结算。
- 上下文用 UTF-8 字节数做保守 token 上界，已统一预检系统提示词、输入和输出配额；尚未使用 DeepSeek 的精确 tokenizer。
- 真实外部副作用对账、供应商侧幂等与恰好一次仍未验证；本地工具结果可由业务回执重放。
- 定价支持 r1/r2 固定，但目录、费用策略与 graph schema 仍缺少完整跨版本迁移验证。
- 当前脚本模板仅提供结构化合成内容；关键词覆盖不代表语义质量、真实版权或真实活动执行可行性。

未实现项不得在评估中标为通过。后续应补外部服务对账、完整跨版本迁移与更大样本线上回归。
