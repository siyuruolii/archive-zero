# Archive Zero eval v1

从项目根目录运行。默认离线，不读取模型密钥、不请求模型 API；所有业务状态与文件操作放在每个样例独立的临时目录，不使用正式用户记忆或数据库。

```powershell
.\.venv\Scripts\python.exe -m evals.runner --repeat 3
```

输出位于 evals/reports/<run_id>/。40 个样例重复 3 次，共 120 次脚本化运行；这验证 Harness、工具和评分器的检查链路，**不代表真实模型有 100% 任务成功率**。参考标签当前是 draft，需要结合你的推荐偏好进行校准。

## 目录

- datasets/reading-v1.json：10 路由推荐、6 精读、6 讨论、8 记忆使用、10 工具/异常样例。任务输入与 expected 验收要求分开，expected 不发送给模型。
- fixtures/topics.json：固定 Topic 与合法标签。
- fixtures/scripts.json：离线控制器响应，仅离线或故障注入案例使用，不作为真实模型输入。
- schema.py：严格数据契约，拒绝重复 ID、非法角色参数与无时区 as_of。
- providers.py：离线控制器、共享的真实 API 尝试次数上限。
- runner.py：执行 Harness、固定输入时间、隔离状态、报告和 CLI。
- graders.py：程序硬检查与人工量表；关键词只提供审阅提示，不作为语义判分依据。
- review.py：导入人工评分，校验运行 ID、用例轮次、输出哈希、评分维度和证据。
- compare.py：同一输入与模式的硬检查版本对比。

## 真实模型评测（会请求 API）

需要明确添加 --live。默认沿用应用的模型配置和各阶段分配，只读取配置，不修改配置。--profile 可指定已有配置 ID，使本次评测统一使用该配置。

先从一个样例开始：

```powershell
.\.venv\Scripts\python.exe -m evals.runner --live --case route-multi-topic --repeat 3 --max-total-model-calls 12
```

最大次数是跨所有样例、重试和阶段共享的 API 尝试次数硬上限，不是金额上限。达到上限后，当前调用明确失败，后续真实任务标记 skipped；失败和跳过不从报告分母移除。未报告的 token 不填零，未配置价格时 cost=null。

样例按 execution 区分：31 个 agent 任务在 --live 模式请求实际模型；9 个 fault_contract 任务始终由故障控制器执行，用来验证损坏输出、权限、取消、预算等边界。报告逐项标明模式，不能把控制器任务当成真实模型成绩。需要对比模型时保持数据、预算和重复次数一致。

## 阅读报告

每次运行单独建目录，不覆盖以前的报告：

- manifest.json：数据/Fixture/源代码哈希、模型非敏感配置、Prompt 版本、固定资料时间、运行模式和预算。
- inputs/：本次 dataset、topics 和 scripts 原始快照，之后可以重放，不依赖最新资料内容。
- <case>-<repeat>.json：终态、硬检查依据、最终结果、工具错误、实际读取范围、文件副作用哈希、调用数、耗时与上下文/缓存观测。
- results.jsonl：逐个落盘的运行记录；中途退出仍保留已完成样例。没有 summary.json 代表整批未完成。
- summary.json、report.md：各类别与每个样例通过次数、全局 API 尝试次数、P50/P95 耗时。离线耗时不是模型服务性能数据。
- reviews-template.json：仅真实 agent 输出的人工评分模板；离线模板为空，防止误把脚本输出评分当成模型验收。

终端退出码：所有硬检查通过为 0；出现 failed、error 或 skipped 为 1；输入/配置错误非零退出。程序成功退出不代表语义质量通过。

## 人工质量评分

以 inputs/dataset.json 的原文、问题和 expected 为依据，阅读对应的最终输出。faithfulness、coverage、uncertainty、relevance 各评 0–3 分，并填写 reviewer、evidence；不得只看关键词是否出现。

复制 reviews-template.json 为 ratings.json，填写后运行：

```powershell
.\.venv\Scripts\python.exe -m evals.review evals/reports/<run_id> ratings.json
```

写出 human-review.json；缺少评分保持待审阅，不视为零分或通过。旧输出哈希、重复评分、越界分数、缺少依据和离线输出均拒绝。该文件只报告人工评分均值与覆盖率，不自动决定产品上线门槛，也不覆盖程序硬检查。

## 重放与版本对比

```powershell
.\.venv\Scripts\python.exe -m evals.runner --dataset evals/reports/<run_id>/inputs/dataset.json --fixtures evals/reports/<run_id>/inputs --repeat 3
.\.venv\Scripts\python.exe -m evals.compare evals/reports/<before> evals/reports/<after> --output comparison.json
```

比较时校验 dataset/fixture 哈希、固定时间、模式和完整样例/轮次集合；输入不同直接拒绝比较。允许源代码或模型配置不同，这正是对比目的。输出包括每项硬检查结果变化、回归数量和耗时差；不自动把硬检查变化推断成语义提升。

## 边界

这是初版人工固定集，尚不能代表真实用户文章分布。推荐参考标签需要你校准；开放式事实准确性仍需人工评审。记忆样例使用真实 MD/SQLite 记忆服务准备全局、Topic、纠正、删除和阅读状态，评估 Agent 对已提供记忆的正确使用；尚未完整覆盖自动画像提取、后台沉淀、跨会话 FTS5 召回与真实联网时效。工具边界覆盖 Harness/执行器，不替代桌面端、Docker/srt OS 隔离验收。

所有 eval 默认禁止命令后端，不提供用户确认回调；模型无法通过评测工具自动执行宿主 Shell。未自动下载网页，没有新增付费评分模型或框架依赖。
