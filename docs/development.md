# Archive Zero 开发与维护

入口：[README](../README.md) · [Agent 地图](../AGENTS.md) · [当前架构](architecture.md)。最后核对：2026-11-09。

## 工作方式

先定位实际调用链和相关测试，再修改；业务通过 Harness，工具能力放进 `agents/tool/` 的业务分组。业务实体与基础类型在 domain/models.py，Agent 输入/输出契约在 agents/models.py，记忆契约在 services/memory/models.py，Schema/业务校验不能因模型偶发失败而关闭。重要新约束应进入可执行检查，局部表达不作无意义限制。

小修改直接实现；复杂变更在相关文档中记录目标、验收、决策与未完成项，完成后更新当前设计即可，不再逐阶段累积同类说明。发生失败追加 [失败账本](failure-log.md)，保留已确认原因与假设的区别。避免同时维护多个“当前架构”。

## 安装与启动

按 README 安装锁定依赖。`scripts/start-desktop.ps1` 调用 npm start，自动先构建再启动。仅启动后端需先设置随机 `AGENT_APP_TOKEN`，再运行 `.\.venv\Scripts\archive-zero-server.exe`（桌面入口会自动生成令牌）；CLI 帮助为 `.\.venv\Scripts\archive-zero.exe --help`。

测试务必使用 tmp_path 或独立 AGENT_DATA_DIR，禁止针对正式 `.local/` 做清空、迁移或故障注入。项目使用 Git 管理；对外发布前检查待提交清单，不提交个人资料、密钥、依赖或构建产物。

## 仓库启动器与更新器

根目录的 `Archive Zero.exe` 是轻量 WPF 启动器，源代码为 `scripts/launcher/Launcher.cs` 和嵌入的 `Launcher.xaml`。在 Windows 使用 `powershell -File scripts/build-launcher.ps1` 重建，需提交源代码、图标及生成的 EXE；完整运行环境不提交。启动器使用进程内 PowerShell 执行策略运行项目脚本，不修改机器执行策略。

`scripts/bootstrap-desktop.ps1` 从固定 HTTPS 地址下载运行环境并核对 `scripts/launcher-runtimes.json` 的 SHA-256。Python 使用官方 [NuGet 运行环境](https://docs.python.org/3.13/using/windows.html#the-nuget-org-packages)，Node 使用 [官方版本文件](https://nodejs.org/dist/v24.22.2/SHASUMS256.txt)。日常模式的运行环境、虚拟环境、下载缓存、指纹与日志都在 `.runtime/`；锁文件防止重复准备。完成相应阶段后才写入成功指纹，失败后可重试。

更新入口由 `SourceUpdate.tsx`、受限 IPC 和 `electron/source-update.cjs` 实现。检查在独立 Node 进程中执行，不阻塞 Electron 主进程；只接受本项目 origin 与 main 分支，拒绝脏工作区与非快进更新。退出旧进程后复查，并从 `.runtime/` 的启动器副本执行更新，避免 Windows 锁住被跟踪的 EXE。不会执行 reset、clean、stash 或强制 checkout，不使用自动交互登录。

验收在独立完整源码副本中运行 `Archive Zero.exe --prepare-only`，再运行 `powershell -File scripts/test-launcher.ps1 -PreparedRoot <副本绝对路径>`，验证缓存复用与 Windows EXE 解锁交接；然后运行 `node desktop/tests/launcher-smoke.cjs <副本绝对路径>`；移除系统 Python/Node PATH，验证项目本地环境和界面。`npm test` 中的 source-update 用真实临时 Git 仓库验证快进、脏工作区、错误分支、错误远程及分叉保护。不能用替身或本机验收替代独立干净 Windows 机器验收。

另运行 `node desktop/tests/exe-startup-smoke.cjs <artifacts 内已准备副本的绝对路径>`，直接执行 EXE，不传 `--prepare-only`，验证实际窗口可见、页面、IPC 和图片。脚本仅临时插桩该可丢弃副本，使用全新隔离资料并在结束时恢复源码；不能指向日常或开发根目录。GUI 应用的 Electron 启动不得使用 `-WindowStyle Hidden`，该选项会影响应用窗口本身；控制台隐藏由外层启动器处理。

日常 EXE 默认 `data/`，开发脚本和源码 CLI 默认 `.dev-data/`。开发副本的 `.development.json` 标记让 EXE 复用 `.venv` 与已安装 Node，日志放 `.dev-data/launcher/`，不新增托管 Python。数据与浏览器 profile 隔离：日常 bootstrap 设置 `AGENT_PROFILE_DIR=data/desktop-profile`，开发入口设置为开发数据目录下的 profile，显式 `AGENT_DATA_DIR` 仍优先。迁移旧资料属于实际数据操作，必须停止旧后端、持有 backend.lock、备份 SQLite 与 MD/JSONL、核对文件摘要；禁止用正式资料进行测试。

运行 `node desktop/tests/instance-isolation-smoke.cjs` 验证两份进程并行、profile 不同与主题数据不互相出现。预览启动器可使用 `Archive Zero.exe --preview <绝对 PNG 路径>`，不启动后端或读取用户资料。

## 验证命令

设置页面可运行 `node desktop/tests/settings-smoke.cjs`：覆盖各栏目、资料库空状态、主页更新器入口、检查失败/可更新/最新/取消确认等状态及窄窗口。更新 UI 使用本机 IPC 替身，不代表真实远程更新验收；Git 防覆盖由 source-update.test 验证。原生测试须同时为数据和 `AGENT_PROFILE_DIR` 指定独立临时目录，避免单实例身份与 GPU 缓存互相干扰。托盘事件单元测试覆盖单击/双击绑定，smoke.cjs 使用真实窗口验证隐藏和最小化后的恢复。

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

原生测试用隔离资料和本机模型替身；第二项真实终止进程并验证手动恢复不重复写文件。需要 npm 依赖中的 Electron/Playwright，不连接正式数据。按变更另选 source-history、workspace-updates、streaming、tool-approval 等原生 smoke 脚本。外观与导航修改另跑 `node desktop/tests/appearance-smoke.cjs` 和 `node desktop/tests/workspace-smoke.cjs`，覆盖角色素材、首页往返草稿/分支保留、专注模式、紧凑布局和跨启动偏好持久化。可离线跑 [Eval](../evals/README.md)：`python -m evals.runner --repeat 3`。

收费真实模型须另有用户授权，在已配置环境显式运行 `pytest tests/live -q --run-live`。真实 Docker 验收要求可信本机镜像，未配置必须跳过并报告；不能用命令参数断言冒充 OS 隔离验收。

## 仓库检查覆盖什么

`scripts/check_repo.py` 检查当前文档的本地链接目标、从 README/AGENTS 可达性、AGENTS 不超过 100 行，以及 Python AST 的绝对/相对导入边界：domain 不依赖运行时、存储或其他上层模块，agents 不导入 services/interfaces，services 不导入 interfaces 或绕过 Harness 直接调用 Loop/上下文/推断/压缩实现，storage 不反向依赖上层，infrastructure 不依赖 agents/services/interfaces。

现有 configuration.py 是配置接线适配器，允许导入 provider、model_router 和 memory_consolidation 的配置契约；例外仅限该文件及三个模块，不放开其他上层依赖。它不是完整静态类型或安全分析，不验证远程链接、章节锚点、动态导入与文档语义新鲜度。新增测试用坏链接、孤立文档、逆向导入和例外越界验证检查器确实会报错。当前未接入远程 CI，发布前需本机执行；未来可将同一命令接入 CI。

## 数据和清理规则

| 路径 | 处理方式 |
| --- | --- |
| data/、旧 .local/ 或自定义日常数据目录 | 正式数据/凭据/会话/记忆，禁止作为缓存清理 |
| .dev-data/ | 仅开发资料与独立桌面 profile，不与日常数据混用 |
| .runtime/ | 启动器下载的运行环境、依赖指纹与日志；不提交，不等同用户资料 |
| .venv/、desktop/node_modules/ | 已安装依赖，保留便于直接工作；分享仓库时不包含 |
| src/*.egg-info/ | 构建生成元数据，可清理；当前安装使用 .venv 中 dist-info，重新构建可再生 |
| artifacts/、evals/reports/ | 生成报告和隔离测试目录；先留必要失败证据再清理，不能提交私密资料 |
| .pytest_cache/、.ruff_cache/、__pycache__/ | 可再生缓存 |
| desktop/dist/ | 可再生构建；开发启动脚本自动重建，直接 electron . 前必须 build |
| tests/、evals/datasets/、evals/fixtures/、依赖锁文件 | 可复现资产，不能当临时文件删掉 |

清理前明确绝对目标与边界，禁止宽泛递归删除；正式数据目录和运行锁永不参与。验证会重新生成少量缓存/报告，这不代表清理失败。

## 已知缺口

尚无生产多租户部署、自动 Judge 或线上 A/B；路由与精读未接入跨进程持久化恢复，真实 Docker/srt 验收仍需配置。开放式输出的语义质量需要独立评审，不能根据结构测试数量推断。

## 仓库文档与发布范围

README 面向使用者；architecture 描述当前设计；本文件描述开发流程；failure-log 记录与项目有关的工程问题。AGENTS.md 为协助修改代码的 coding agent 提供导航与约束，不会被应用运行时读取。运行时系统提示由 agents/prompts.py 和 context.py 装配。

个人工作资料、一次性本机维护记录和诊断原件不属于源码交付，应留在仓库外。发布前检查 git ls-files、文档链接和包内文件清单；.gitignore 不会移除已经跟踪或已发布历史中的内容。需要清除已发布个人文件时，先保存本地副本，再经授权清理分支和标签历史，并通知已有克隆者重新同步。
