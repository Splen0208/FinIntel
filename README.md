# FinIntel

**Your personal research agent — from scattered information to your next step.**

FinIntel 是面向求职与行业研究的个人研究 Agent：用 **BOSS 直聘 + 小红书**了解岗位、求职分享和职业讨论，结合自己的简历生成面试重点与行动清单；用 **X** 搜集海外行业观点，为行业研究和竞品调研补充视角。

[![Watch the 90-second FinIntel demo](docs/assets/finintel-demo-preview.png)](https://github.com/Splen0208/FinIntel/releases/download/v0.1.0/finintel-promo-main-90s.mp4)

**[▶ Watch the 90-second demo / 观看 90 秒介绍](https://github.com/Splen0208/FinIntel/releases/download/v0.1.0/finintel-promo-main-90s.mp4)** · [下载与版本说明](https://github.com/Splen0208/FinIntel/releases/tag/v0.1.0) · [安装指南](docs/QUICKSTART.md)

中文讲解 · English subtitles · 1080p。视频展示真实界面，简历使用合成示例。

## What you can do / 可以用它做什么

- **准备一次面试：**搜索目标岗位 → 阅读原始 JD → 选择相关小红书材料 → 导入简历 → 查看匹配 → 生成练习与准备任务 → 导出报告。
- **研究公司与同行：**从公开招聘中整理工作方向，结合笔记与评论寻找值得继续研究的话题。
- **补充海外行业视角：**单独切换 X，搜集海外观点与热点，保存带原文引用的研究结果。

每次研究保存原文快照；结论可以展开引用与上下文。Agent 把工具采集、证据保存、模型分析和准备行动连接起来。

## Try it first / 先体验

需要 Python 3.11+。完整三平台链路目前在 Windows + Google Chrome 上验证；其他系统的专用浏览器管理尚未验证。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe finintel.py --demo
```

打开 http://127.0.0.1:8766/ ，选择 BOSS，输入 `消费金融 产品经理`，点击“搜索并整理”。此模式使用合成岗位；新安装未配置模型时使用规则分析，无需平台账号或模型密钥。已有模型配置时会使用该模型。X 和小红书仍需要真实接入。退出后用 `finintel.py` 启动真实模式；演示材料保留来源标记。端口被占用时可加 `--port 8780`。

## Connect your own sources / 接入自己的账号

安装 `requirements-platforms.txt`，在界面设置里配置 OpenAI 兼容模型的地址、模型名和密钥；BOSS 使用本项目的专用 Chrome 窗口，X/小红书可使用 OpenCLI 与自己已经登录的 Chrome。逐步操作见 [Quickstart](docs/QUICKSTART.md)。真实采集失败会保留原因与已取得材料，不会自动改用演示数据。

| 来源 | 已实现的链路 | 证据边界 |
| --- | --- | --- |
| BOSS 直聘 | 关键词搜索 → 详情页职位正文 → 岗位/公司方向 → 引用查看 | 完整正文须匹配公开岗位 ID、详情 URL、可见且未裁剪的描述区；卡片保留“部分正文” |
| X | Twitter CLI；特定连接/契约错误时使用 OpenCLI 关键词搜索 → 帖子 → 主题与原文 | 不以推荐流替代搜索；缺失/部分材料如实标记 |
| 小红书 | OpenCLI 搜索 → 笔记与评论 → 主题与引用 | 评论数是本批采集数量，不代表全站或全部评论 |

从 BOSS 搜索结果点击“准备此岗位”，冻结本次 JD；把 X／小红书结论的原文证据选入同一工作区，再导入简历、做匹配并生成准备计划。公开讨论单独生成有引文与局限说明的背景练习问题，不能冒充岗位要求、本人经历或招聘方真实题。更换材料后，旧计划会标为过期。完整操作见 Quickstart。

## How it works

```text
Your query → Source adapter → Immutable evidence snapshots (SQLite)
           → Configured model → Schema + exact quote validation
           → Findings / original context / human review / export
```

它是一种面向个人研究任务的 Agent 工作流：有工具、持久状态、预算、恢复和人工边界。编排是受控流程；应用与材料保存在本机，模型分析会把选定证据发送给配置的外部模型服务。

## Develop

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

前端是原生 HTML/CSS/JavaScript，由 FastAPI 提供，无前端构建步骤。后端启动会自动执行 SQLite 版本迁移。代码结构与约束见 [Architecture](docs/ARCHITECTURE.md)，贡献方式见 [CONTRIBUTING](CONTRIBUTING.md)。

## Data and release

仅供单用户本机使用，默认监听 `127.0.0.1`，尚无公网部署所需的多用户认证。`.local/` 包含数据库、模型密钥、账号资料、浏览器登录与简历；`outputs/` 包含自己的报告。不要上传这些目录。更多说明见 [SECURITY](SECURITY.md)。

本项目源代码使用 [MIT](LICENSE)。第三方工具遵循各自许可证与平台规则，见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。干净发布包用 `python scripts/build_release.py --output <目录>` 生成；仅包含源代码、合成测试材料与公开文档。
