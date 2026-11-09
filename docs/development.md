# Archive Zero 开发与维护

入口：[README](../README.md) · [Agent 地图](../AGENTS.md) · [当前架构](architecture.md)。最后核对：2026-11-09。

## 工作方式

先定位实际调用链和相关测试，再修改；业务通过 Harness，工具能力放进 `agents/tool/` 的业务分组。业务实体与基础类型在 domain/models.py，Agent 输入/输出契约在 agents/models.py，记忆契约在 services/memory/models.py，Schema/业务校验不能因模型偶发失败而关闭。重要新约束应进入可执行检查，局部表达不作无意义限制。

小修改直接实现；复杂变更在相关文档中记录目标、验收、决策与未完成项，完成后更新当前设计即可，不再逐阶段累积同类说明。发生失败追加 [失败账本](failure-log.md)，保留已确认原因与假设的区别。避免同时维护多个“当前架构”。

## 安装与启动

按 README 安装锁定依赖。`scripts/start-desktop.ps1` 调用 npm start，自动先构建再启动。仅启动后端需先设置随机 `AGENT_APP_TOKEN`，再运行 `.\.venv\Scripts\archive-zero-server.exe`（桌面入口会自动生成令牌）；CLI 帮助为 `.\.venv\Scripts\archive-zero.exe --help`。

测试务必使用 tmp_path 或独立 AGENT_DATA_DIR，禁止针对正式 `.local/` 做清空、迁移或故障注入。项目使用 Git 管理；对外发布前检查待提交清单，不提交个人资料、密钥、依赖或构建产物。

## 验证命令

项目根目录，默认不调用收费模型：

```powershell
.\.venv\Scripts\python.exe scripts/check_repo.py
.\.venv\Scripts\python.exe -m ruff check src tests evals scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests evals scripts
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --junitxml=artifacts/repo-cleanup-tests.xml
npm --prefix desktop run build
npm --prefix desktop test
```

首次从干净目录运行原生测试前先创建 artifacts 目录：

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
Push-Location desktop
node tests/smoke.cjs
node tests/recovery-smoke.cjs
Pop-Location
```

原生测试用隔离资料和本机模型替身；第二项真实终止进程并验证手动恢复不重复写文件。需要 npm 依赖中的 Electron/Playwright，不连接正式数据。按变更另选 source-history、workspace-updates、streaming、tool-approval 等原生 smoke 脚本。可离线跑 [Eval](../evals/README.md)：`python -m evals.runner --repeat 3`。

收费真实模型须另有用户授权，在已配置环境显式运行 `pytest tests/live -q --run-live`。真实 Docker 验收要求可信本机镜像，未配置必须跳过并报告；不能用命令参数断言冒充 OS 隔离验收。

## 仓库检查覆盖什么

`scripts/check_repo.py` 检查当前文档的本地链接目标、从 README/AGENTS 可达性、AGENTS 不超过 100 行，以及 Python AST 的绝对/相对导入边界：domain 不依赖运行时、存储或其他上层模块，agents 不导入 services/interfaces，services 不导入 interfaces 或绕过 Harness 直接调用 Loop/上下文/推断/压缩实现，storage 不反向依赖上层，infrastructure 不依赖 agents/services/interfaces。

现有 configuration.py 是配置接线适配器，允许导入 provider、model_router 和 memory_consolidation 的配置契约；例外仅限该文件及三个模块，不放开其他上层依赖。它不是完整静态类型或安全分析，不验证远程链接、章节锚点、动态导入与文档语义新鲜度。新增测试用坏链接、孤立文档、逆向导入和例外越界验证检查器确实会报错。当前未接入远程 CI，发布前需本机执行；未来可将同一命令接入 CI。

## 数据和清理规则

| 路径 | 处理方式 |
| --- | --- |
| .local/ 或自定义数据目录 | 正式数据/凭据/会话/记忆，禁止作为缓存清理 |
| .venv/、desktop/node_modules/ | 已安装依赖，保留便于直接工作；分享仓库时不包含 |
| src/*.egg-info/ | 构建生成元数据，可清理；当前安装使用 .venv 中 dist-info，重新构建可再生 |
| artifacts/、evals/reports/ | 生成报告和隔离测试目录；先留必要失败证据再清理，不能提交私密资料 |
| .pytest_cache/、.ruff_cache/、__pycache__/ | 可再生缓存 |
| desktop/dist/ | 可再生构建；开发启动脚本自动重建，直接 electron . 前必须 build |
| tests/、evals/datasets/、evals/fixtures/、依赖锁文件 | 可复现资产，不能当临时文件删掉 |

清理前明确绝对目标与边界，禁止宽泛递归删除；正式数据目录和运行锁永不参与。验证会重新生成少量缓存/报告，这不代表清理失败。

## 已知缺口

尚无生产多租户部署、自动 Judge 或线上 A/B；路由与精读未接入跨进程持久化恢复，真实 Docker/srt 验收仍需配置。开放式输出的语义质量需要独立评审，不能根据结构测试数量推断。

## Windows 便携发布

源码发行包为 archive-zero，桌面包为 archive-zero-desktop。旧命令和内部 reading_agent 导入名保留兼容。发布包自带冻结 Python 后端与 Electron，不需要使用者安装 Python/Node。构建与启动方式见 README。

安装 requirements-build.lock 后运行 scripts/build_release.py。构建器按明确清单收集前端、Electron、冻结后端和许可证，不收集个人数据、工作资料或凭据。发布产物位于被忽略的 release/，附 SHA256SUMS.txt。首版为未签名 Windows x64 ZIP 便携包，不含自动更新或安装程序。

发布模式由 desktop/electron/runtime.cjs 选择内置后端，并明确传入前端资源与可写用户数据目录。命令工作进程通过 --shell-worker 分派，不能把冻结 EXE 当作普通 Python 解释器调用。

验收命令：node desktop/tests/release-smoke.cjs <解压后的 EXE 路径>。测试使用空白独立资料目录，移除 Python/Node 的 PATH、移除 PYTHONPATH，设置无效 AGENT_PYTHON，验证真实 EXE、界面、Topic 写入和关闭隐藏到托盘。这不等同独立干净 Windows 虚拟机验收。

## 仓库文档与发布范围

README 面向使用者；architecture 描述当前设计；本文件描述开发流程；failure-log 记录与项目有关的工程问题。AGENTS.md 为协助修改代码的 coding agent 提供导航与约束，不会被应用运行时读取。运行时系统提示由 agents/prompts.py 和 context.py 装配。

个人工作资料、一次性本机维护记录和诊断原件不属于源码交付，应留在仓库外。发布前检查 git ls-files、文档链接和包内文件清单；.gitignore 不会移除已经跟踪或已发布历史中的内容。需要清除已发布个人文件时，先保存本地副本，再经授权清理分支和标签历史，并通知已有克隆者重新同步。
