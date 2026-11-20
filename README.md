# 知序 · 学习工作台（v3.2）

单人全栈、本地运行的一站式学习平台：聊天首页 + 真实 RAG + 知识区与多级知识树 + 知识关系星图 + 思维导图（结构式/概念式）+ AI 出题 + 论文格式检查修正。

本版在 v3.1 基础上增加：知识树与节点范围检索、问题清洗/课程同义词/标题与表格重排、引用原文哈希与漂移检测、Windows DPAPI 密钥加密、本机接口防护和检索回归测试；并新增解析质量评分、AI 导图失败自动回退规则版、导图折叠与 SVG/PNG 客户端导出。

> 项目全景介绍（定位、能力地图、架构、核心机制、已知限制与路线图）见 [docs/项目介绍.md](docs/项目介绍.md)。

技术栈：Python 3.11 · FastAPI · SQLite FTS5 · 单文件前端（无框架/CDN）。规格见 `docs/开发规格与Agent提示词-v3.1.md`。

## 1. 安装

```powershell
cd zhixu-study-workbench
python -m venv .venv
.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -r requirements.txt
```

> 本机 pip 默认配置指向阿里云镜像（当前返回 403），所以安装命令显式指定官方源；该参数只对本次命令生效，不改全局配置。

## 2. 启动

双击 `start.bat`（或执行）：

```powershell
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8788
```

浏览器打开 <http://127.0.0.1:8788>。首次启动自动创建空的 `data/` 数据库（0 条业务数据）。

## 3. 首次配置

1. 打开「设置」，填写 `base_url`、`api_key`、`model`（DeepSeek 官方：`base_url=https://api.deepseek.com`、`model=deepseek-flash`，即 V4.1 Flash；其他 OpenAI 兼容厂商按各自文档填写）；
2. 点「测试连通」；
3. 点「PDF 转换自检」（依赖本机 Microsoft Word 或 WPS 的 COM 自动化）。

密钥只保存在 `data/config.json`（已 gitignore），接口只回掩码。

### 配置两个 Supabase 登录账号

1. 在 Supabase 新建项目。在 **Authentication → Users** 预建两个邮箱密码账号，确认邮箱并记录两个用户 UUID。指定其中一个作为主账号。
2. 在 **Authentication → Sign In / Providers** 启用 Email，关闭 **Allow new users to sign up** 和 **Allow anonymous sign-ins**。获取项目 URL 和 publishable key。旧项目也可用 `SUPABASE_ANON_KEY` 提供 legacy anon key。[Supabase 登录配置](https://supabase.com/docs/guides/auth/general-configuration) · [Supabase API keys](https://supabase.com/docs/guides/api/api-keys)
3. 首次迁移前关闭原本运行的知序实例。在启动知序的同一 PowerShell 窗口设置以下环境变量，再重新启动。

```powershell
$env:ZHIXU_AUTH_REQUIRED = "1"
$env:SUPABASE_URL = "https://<项目引用>.supabase.co"
$env:SUPABASE_PUBLISHABLE_KEY = "<publishable key>"
$env:ZHIXU_AUTH_USER_IDS = "<主账号 UUID>,<第二账号 UUID>"
$env:ZHIXU_MAIN_USER_ID = "<主账号 UUID>"
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8788
```

Supabase 处理邮箱和密码认证。资料、索引、笔记、对话、试卷、论文和导出文件存放在本机 `data/accounts/<用户 UUID>/`。两个账号共用本机模型配置，主账号负责保存和测试配置。浏览器持有本地会话 Cookie，登录最长保留 12 小时。应用进程重启后需重新登录，请使用单进程启动命令。

首次使用完整配置启动时，程序先把旧数据库、上传文件和导出文件备份到 `data/auth-backups/legacy-v1/`，再复制到主账号。原位置和备份中都保留迁移前的数据。第二账号从空工作台开始。后续重启沿用各自的账户数据。迁移完成后若登录环境变量缺失，受保护的接口会返回配置错误。全新安装在没有任何登录设置时使用本机演示模式。

## 4. 使用流程

1. 「知识库」新建知识区（如 信号 / 数电 / 大物 / 2数），再在知识区内创建多级知识节点；
2. 上传资料（PDF/DOCX/PPTX/MD/TXT/图片，≤20MB），可选择所属知识节点；扫描件自动 OCR（最多 30 页）；
3. 左侧「+ 新建会话」→ 选择知识区和知识节点 → 提问。选了节点时只检索该节点及其子节点。命中课件时可点击引用查看原文，零命中时 AI 会提供标有「通用讲解」的回答。
   命中的 PDF 页和 PPTX 幻灯片会在生成回答前显示缩略图，点击可放大。PPTX 预览需要 Windows 上的 PowerPoint，预览不可用时可下载原文件。
4. 「思维导图」选择资料 → 结构式生成 / AI 生成（概念导图）→ 折叠/展开节点、编辑保存、重新生成（保留手改）→ 导出 SVG/PNG；AI 生成失败时自动回退规则版并提示原因；
5. 「作业」→ 论文格式修正（上传 DOCX → 检查 → 确认执行 → 下载修正版/报告）或教师出题成卷（单选/多选/判断/计算题量自由组合，单次 1–20 题 → MathML 公式预览 → 导出学生版 DOCX/PDF）。
6. 「学霸笔记」→ 手动输入或从知识区用 AI 整理 → 选择「横线笔记本」或「手账皮革本」→ 保存笔记或导出可独立打开的 HTML。手账支持按钮和方向键翻页。

「学霸笔记」采用 [AI_Animation 的 scholar-notes](https://github.com/Unclecheng-li/AI_Animation/tree/master/skills/scholar-notes) 的 MIT 模板风格。原始 Skill 和许可证位于 [skills/scholar-notes](skills/scholar-notes/)。

## 5. 验收

- 自动化冒烟：`.venv\Scripts\python.exe scripts\smoke.py`（使用临时数据目录，PDF 相关检查依赖本机 Word/WPS COM）
- 检索与安全回归：`.venv\Scripts\python.exe scripts\regression.py`（41 项，不调用真实模型，不占用端口）
- 本机双账号回归：`.venv\Scripts\python.exe scripts\test_local_auth.py`（模拟 Supabase 登录服务，验证旧数据备份、会话、账号隔离、检索、预览和导出）
- v3.2 增量单测：`scripts\test_v32_quality.py`（14 项）、`scripts\test_v32_mindmap_fallback.py`（14 项），均使用临时数据目录
- 笔记与课堂录音单测：`scripts\test_note_recap.py`（17 项，覆盖整课分节摘要、长转写、非法输出、正文编辑与录音静态检查）
- 知识星图静态测试：`scripts\test_library_graph.py`（13 项，覆盖动态力导向、星图数据、筛选缩放拖拽、详情和列表树）
- 出题与公式单测：`scripts\test_quiz_configurable.py`（27 项，覆盖题型题量、教师命题 Skill、北邮风格 Skill、公式校验与 OMML 导出）
- 演示样例资料：`.venv\Scripts\python.exe scripts\make_samples.py`（在 `data/samples/` 生成 MD/TXT/DOCX/PPTX/文本层 PDF/扫描件 PDF 六种格式，逐个在网页上传即可）
- 真实模型验收：`.venv\Scripts\python.exe scripts\verify_model.py --base-url ... --model ...`（Key 只在本进程内存或环境变量，不落盘；验证 RAG 引用、M2 导图、6 题试卷、论文检查、token 用量）
- 手动验收清单：`docs/acceptance.md`（对应规格第 19 节 D/R/K/C/M/Q/P/X）

## 6. 目录

```
app.py  db.py  rag.py  llm.py  mindmap.py  quiz.py  paper.py  officedoc.py  config.py  tree.py  quality.py
web/index.html          # 单文件前端（含导图折叠与 SVG/PNG 导出）
skills/                 # 7 个产品 skill（含教师命题、北邮风格与课堂摘要）
scripts/smoke.py        # 自动化验收（临时 DATA_DIR，不碰真实数据）
scripts/regression.py   # 检索、知识树、引用漂移、DPAPI、安全基线、v3.2 静态检查回归
scripts/test_quiz_configurable.py         # 可配置出题与公式安全单测
scripts/test_v32_quality.py             # 解析质量评分单测
scripts/test_v32_mindmap_fallback.py    # 导图截断与自动回退单测
docs/                   # 规格、接口约定、验收清单
data/                   # 运行时生成（gitignore）：app.db / uploads / exports / tmp / config.json
```

## 7. 已知限制

- 无流式输出（生成期间显示“正在检索资料…/正在生成回答…”）；
- 检索仍为 SQLite FTS5 + 中文 bigram + 课程同义词和标题/表格重排，**不是向量语义检索**；换用完全不同的说法仍可能召回不足；
- 知识树目前支持多级节点、资料归属、整棵子树检索；暂不支持拖拽移动节点；
- 引用会保存原文哈希并在重建索引后检测漂移，但如果原文件被替换且原 chunk 完全消失，只能显示新内容或快照；
- API Key 使用 Windows DPAPI 绑定当前 Windows 用户；换电脑或换系统用户需要重新填写；
- OCR 仅前 30 页，手写体不保证；
- PDF 依赖本机 Word/WPS COM；两者都不可用时导出报 `pdf_engine_unavailable`；
- 仅桌面（≥1280px），无移动端适配；
- 解析质量分是启发式规则（乱码率、有效字符占比），只用于提示与检索降权，不等同于内容正确性；
- 导图导出为浏览器端 SVG/PNG，只含当前可见节点；不支持 Markdown 导出；试卷只导出学生版。
