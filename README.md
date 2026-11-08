# Archive Zero · 零号档案

一个面向个人的阅读 Agent：从 RSS 收集材料，按 Topic 筛选和摘要，按需精读，再围绕原文讨论，把阅读线索沉淀为可追溯的记忆。

**Windows 桌面应用 · Python / FastAPI / SQLite · React / TypeScript / Electron · MIT**

当前为单用户本地预览版。仓库目前私有，源码与 Releases 仅对已授权协作者可见。

## 直接使用：下载 Windows 发布包

前往 [Releases](https://github.com/siyuruolii/archive-zero/releases)，下载 Windows x64 的 ZIP 和 SHA256SUMS.txt。

1. 完整解压 ZIP 到可访问的目录。
2. 双击其中的 **Archive Zero.exe**；不要只取出单个 EXE，旁边的资源和 backend 必须保留。
3. 左下角“设置”添加兼容 Chat Completions 的模型接口、模型名和你自己的 API Key。
4. 创建 Topic、添加 RSS 来源，选择需要导入的历史文章，开始阅读和讨论。

发布包自带桌面运行环境和 Python 后端，使用者不必安装 Python、Node 或 npm。API 调用费用由所配置的供应商收取。保存模型配置不代表已经完成连接验证。

首版包未做代码签名，也不是安装程序；Windows 可能显示“未知发布者”。请核对仓库来源与 SHA-256，不要关闭系统防护。可用 PowerShell `Get-FileHash .\Archive-Zero-*.zip -Algorithm SHA256` 与校验文件比较。

关闭窗口会隐藏到右下角托盘，任务继续运行；双击托盘恢复，通过托盘菜单退出。发布版数据保存在 `%APPDATA%\reading-agent-desktop\data`，与程序解压目录分离；升级时退出应用、备份数据、解压新包，不要覆盖或删除数据目录。模型密钥在 Windows 使用当前用户 DPAPI 加密。

## 开发启动：从源码运行

需要 Windows x64、Python 3.13（项目最低要求 3.11）、Node 24/npm 和 Git。开发者安装依赖后可修改代码。

```powershell
git clone https://github.com/siyuruolii/archive-zero.git
cd archive-zero
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm --prefix desktop ci
.\scripts\start-desktop.ps1
```

启动脚本会先构建前端，然后打开桌面窗口并自动启动本机后端。开发版使用仓库内 `.local/`，发布版使用上述独立用户目录；`AGENT_DATA_DIR` 可显式指定其他数据目录。

也可配置 `.env.example` 所列的进程环境变量，程序不会自动加载 `.env`。CLI 为 `archive-zero` / `archive-zero-server`，旧命令 `reading-agent` / `reading-agent-server` 保留兼容；内部 Python 导入名仍是 `reading_agent`。

## 已实现的能力

- Topic 工作区、标签、RSS/Atom 来源、订阅时间过滤、历史文章勾选导入。
- 路由摘要、推荐与按需精读；分段读取原文，以实际字符范围校验阅读证据。
- 精读和讨论流式展示，支持停止、分支会话，以及讨论意外中断后的手动续跑。
- 共用 Harness 管理上下文、按需/强制压缩、模型和工具预算、结果校验及记忆收尾。
- 工作上下文、Markdown 长期记忆和 JSONL 会话记忆；SQLite 管理元数据与可重建检索索引。
- 工具渐进加载、文件工作区、执行前权限检查、命令逐次确认和未知副作用保护。
- 分阶段模型设置、上下文/用量/缓存观测、固定 eval 和人工质量评分入口。

## 本地验证

```powershell
.\.venv\Scripts\python.exe scripts/check_repo.py
.\.venv\Scripts\python.exe -m ruff check src tests evals scripts
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix desktop run build
npm --prefix desktop test
.\.venv\Scripts\python.exe -m evals.runner --repeat 3
```

默认测试不请求收费模型；真实模型测试必须明确使用 `--run-live`，会产生供应商费用。离线 eval 的脚本化成功不代表模型回答准确率。真实 Docker/srt 隔离环境验收需另行配置，缺少环境时如实跳过。

## 自行构建 Windows 便携发布包

在已安装开发依赖的 Windows x64 环境运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.lock
.\.venv\Scripts\python.exe scripts/build_release.py
```

产物位于 `release/`：便携 ZIP、SHA256SUMS.txt 和解压目录。相同版本目录存在时构建器拒绝覆盖，请先移出旧产物。构建仅从明确列出的前端、Electron 运行文件和冻结后端收集，不打包开发机 `.local/`、密钥、会话、测试目录或 `.venv/`。

PyInstaller 冻结后端，Electron 发布目录内包含 `resources/backend` 与 `resources/app`。运行时使用独立可写用户目录，不向安装/解压目录写数据库。构建和端到端验收说明见开发指南；源码启动与发布包启动分别验证。

## 仓库地图

| 入口 | 内容 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 编码 Agent 的导航与约束 |
| [架构](docs/architecture.md) | Harness、模块关系、数据权威源 |
| [开发指南](docs/development.md) | 安装、验证、维护与发布说明 |
| [失败账本](docs/failure-log.md) | 失败、原因、修复和验收边界 |
| [Eval 操作](evals/README.md) | 固定数据集、评分和版本比较 |

`src/reading_agent/` 是后端；`desktop/` 是桌面；`tests/` 是回归；`evals/` 是固定评测；`scripts/` 是启动、检查与构建入口。

## 使用边界与许可

本地保存不等于离线：远程模型会收到相关材料、讨论和合法记忆。不要将 API Key、个人数据库、会话、记忆或历史诊断备份提交到仓库。生成依赖、缓存和发布产物已在 `.gitignore` 中排除。

命令执行逐次确认；本机 Shell 不提供操作系统隔离。路由/精读跨进程续跑、通用联网搜索、多用户部署与开放式模型质量验收尚未完成。当前不提供自动更新，不承诺生产 SLA、彻底防注入或任意副作用恰好执行一次。

项目使用 [MIT License](LICENSE)。第三方运行组件遵守各自许可证，发布包保留其许可文件。
