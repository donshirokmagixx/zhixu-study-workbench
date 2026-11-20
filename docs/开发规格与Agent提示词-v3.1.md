# 开发规格与 Agent 提示词 v3.1（规格书式，零自由发挥）

> 版本：v3.1｜生成时间：2026-11-18 18:00
> v3 → v3.1 变更：新增第 4 个产品 skill `mindmap-builder`；思维导图升级为 **M1 结构式（不调模型）+ M2 概念式（调模型）** 双模式；补充 M2 的证据选择、校验、截断、失败与验收项；`mindmaps` 表增加 `mode`，`mindmap_nodes` 表增加 `unverified`。
> 使用方法：把下面 `===== 提示词开始 =====` 之后的内容整段复制给编码 Agent。不要附带本文前半部分。
> 前置：`skills/` 下已有 4 个 skill（`rag-evidence-reader`、`paper-format-corrector`、`grounded-quiz-generator`、`mindmap-builder`），禁止修改其内容。

## 一、三轮边界确认记录（用户选择）

| 轮次 | 决策项 | 结果 |
|---|---|---|
| 1 | 开发范围 | **全模块**：聊天/RAG/知识库 + 导图 + 出题 + 论文格式修正（含 DOCX、公式 Word 导出） |
| 1 | 技术栈 | FastAPI + SQLite FTS5 + 改造现有单文件前端 |
| 1 | 运行形态 | 本机一条命令启动，浏览器开 localhost |
| 1 | 模型接口 | OpenAI 兼容接口，填 base_url / api_key / model |
| 1 | 资料格式 | PDF（文本层）、DOCX、Markdown/TXT、PPTX、扫描件/图片 OCR |
| 2 | 会话绑定 | 会话只绑 1 个知识区；切区必须新建会话 |
| 2 | 检索实现 | 只用 FTS5 关键词检索（中文 bigram 兜底），不做 embedding/向量 |
| 2 | OCR | rapidocr-onnxruntime（离线） |
| 2 | 论文修正 | 支持导入学校模板 JSON 覆盖；覆盖标题层级/正文/页边距/题注/目录页码页眉页脚/参考文献引文校验；参考 `skills/paper-format-corrector` |
| 2 | 导出 | 论文修正版 DOCX；学生版试卷 DOCX；以上导出另出 PDF |
| 2 | 出题 | 客观题四类 + 简答/计算 + 结合导图节点定考点 |
| 3 | 首页布局 | 聊天为主 + 左侧会话列表 + 顶部知识区选择 |
| 3 | 功能入口 | 保留独立"知识库/思维导图/作业"二级入口 |
| 3 | 试卷口径 | 只导出学生版；教师版不导出；参数固定（固定 6 题） |
| 3 | 论文流程 | 检查→确认→执行→下载；检查报告可导出 |
| 3 | 提示词粒度 | 规格书式，零自由发挥 |
| 3+ | 导图 | **v3.1 定稿：双模式**——M1 结构式（不调模型）+ M2 概念式（调用 `mindmap-builder` skill） |

## 二、环境核实（本机实测，作为规格依据）

- Python：`D:\Python311\python.exe`，3.11.5 ✅
- Microsoft Word：`C:\Program Files\Microsoft Office\Root\Office16\WINWORD.EXE`，`Word.Application` COM 已注册 ✅
- WPS Office：`D:\对梁杰有用的东西\WPS Office\12.1.0.28043\office6\wps.exe`，`KWps.Application` COM 已注册 ✅
- Chrome 157 ✅
- pandoc：未安装 ❌ → 公式转 OMML 不使用 pandoc
- 结论：PDF 转换**优先 Word COM，失败再用 WPS COM**；公式 OMML 用 `latex2mathml + mathml2omml`

## 三、写死在规格里的三个决定（如需改动请直接指出）

1. **试卷固定 6 题**：单选/多选/判断/填空/简答/计算各 1 题；页面不提供题量/难度参数。
2. **品牌沿用"知序"**。
3. **导图双模式**：M1 结构式零模型；M2 概念式调用 `mindmap-builder`，一次生成一次调用，失败不自动降级。

---

===== 提示词开始 =====

你是本项目的全栈开发 Agent。**单人开发**：不执行 A/B/C 分工与 CodeArts 分支/MR 流程。唯一规范是本提示词；工作区其他文档与本文冲突处以本文为准（但"不提交密钥/真实资料、不伪造验证结果"仍然适用）。

## 0. 硬性规则（先读）

1. 不得增加本文未列出的功能、页面、接口、数据表、依赖或技术栈；不得更换方案；不得自行"优化"。
2. 做不到的验收项，必须在报告中写"未完成 + 原因"；**不得用替代方案冒充完成**。
3. 所有界面数据必须来自真实数据库/真实日志；**禁止任何种子、演示、示例数据进入正式界面**。
4. 规格中的文件名、表名、接口路径、字段名、状态枚举、界面文案、数值参数，**一个字都不许改**。
5. 完成后必须按第 20 节格式回复；每阶段自测通过才能进入下一阶段。

## 1. 目标与范围

把 `D:\codeart\project\huawei project` 现有的单文件 `index.html`（工作台式 UI 原型 + 硬编码假数据）改造为"**首页即聊天**"的真实 RAG 学习平台，含全部模块。

**必须实现（范围内）**
- 聊天首页 + OpenAI 兼容模型调用 + 按知识区检索 + 带原文引用的回答
- 资料上传（PDF/DOCX/MD/TXT/PPTX/图片与扫描件 OCR）→ 知识区 → 解析 → 切块 → FTS5 索引
- 知识库管理（知识区增删改、文档状态、重建索引、删除）
- 思维导图双模式：M1 结构式（不调模型）+ M2 概念式（调用 `mindmap-builder` skill）；可编辑、保存、节点溯源、重新生成保留手改
- AI 出题（固定 6 题、页面显示答案解析、导出学生版 DOCX+PDF）
- 论文格式检查与 DOCX 修正（模板 JSON 覆盖、目录/页码/参考文献规则、修正版 DOCX+PDF、检查报告 PDF）
- 设置页（模型配置、连通测试、PDF 自检、真实用量记录、清空所有数据）

**禁止实现（范围外）**
账号/登录/多用户；embedding/向量检索；SSE 流式输出；演示模式/种子数据；总览页/任务/打卡/统计图表/最近学习；导图导出（PNG/SVG/MD/OPML）；教师版试卷导出；Word 公式编辑器 UI；Obsidian；多知识区绑定；移动端适配（只保证桌面 ≥1280px）；Docker/云部署；异步任务队列（解析同步执行）；学校模板可视化编辑（只收 JSON）；手写体 OCR 质量承诺；论文除 DOCX 外的输入；聊天导出。

## 2. 技术栈（锁定）

- Python 3.11（`D:\Python311\python.exe`）
- 后端：FastAPI + uvicorn；数据库：Python 内置 `sqlite3`，必须启用 FTS5（启动时自检，失败则报错退出，不允许降级为 LIKE）
- 前端：单文件 `web/index.html`，HTML + 内联 CSS/JS，无框架、无 CDN、无外部字体/图片
- `requirements.txt` 内容固定为以下 12 个包（不写版本号）：
  `fastapi`、`uvicorn`、`python-multipart`、`httpx`、`pypdf`、`PyMuPDF`、`python-docx`、`python-pptx`、`rapidocr-onnxruntime`、`pywin32`、`latex2mathml`、`mathml2omml`

## 3. 目录与文件（精确）

```
<repo root>
  app.py             # FastAPI 入口 + 全部 HTTP 路由（唯一注册处）
  db.py              # 建库、连接、建表、查询封装
  rag.py             # 解析/OCR/切块/FTS5 索引/检索/上下文组装
  llm.py             # OpenAI 兼容调用、skill 加载与哈希、JSON 解析与校验
  mindmap.py         # M1 结构式生成、M2 调用与校验、重新生成合并
  quiz.py            # 出题、OMML 公式、学生版 DOCX 生成
  paper.py           # 论文检查、执行修正、报告生成
  officedoc.py       # Word/WPS COM 转 PDF、转换自检
  config.py          # 路径、常量、data/config.json 读写
  web/index.html     # 唯一前端页面（内联 CSS/JS）
  skills/            # 4 个产品 skill（禁止修改内容）
    rag-evidence-reader/SKILL.md
    paper-format-corrector/SKILL.md + references/ + assets/
    grounded-quiz-generator/SKILL.md
    mindmap-builder/SKILL.md
  scripts/smoke.py   # 自动化验收脚本（使用临时 DATA_DIR，不碰真实数据）
  docs/acceptance.md # 手动验收清单（与第 19 节一致）
  requirements.txt
  start.bat
  .gitignore
  README.md
  data/              # 运行时生成：app.db、uploads/、exports/、tmp/、config.json（gitignore）
```

- 删除旧 `index.html` 与其中全部视图代码、演示资料、预置问答、题库、论文样例、默认任务/导图/统计。
- 不得新增其他 `.py` 文件；每个文件职责如注释所示。

## 4. 启动与配置

- `start.bat` 内容固定：
  ```bat
  @echo off
  cd /d %~dp0
  python -m uvicorn app:app --host 127.0.0.1 --port 8765
  pause
  ```
- 启动后浏览器打开 `http://127.0.0.1:8765`；不做自动打开浏览器。
- 首次启动自动创建 `data/` 与空数据库（**0 条业务数据**）。
- 配置存 `data/config.json`：`{"base_url":"","api_key":"","model":""}`；接口返回 `api_key` 时只返回掩码，永不明文回传。
- `.gitignore` 固定包含：`data/`、`__pycache__/`、`*.pyc`、`.venv/`、`*.db`
- `README.md` 必须含：安装命令、启动命令、访问地址、模型配置步骤、验收步骤、已知限制。

## 5. 数据库结构（db.py 建表，字段名固定）

```sql
zones(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
documents(id TEXT PRIMARY KEY, zone_id TEXT NOT NULL, filename TEXT NOT NULL, ext TEXT NOT NULL,
          size INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
          warning TEXT, page_count INTEGER, chunk_count INTEGER, created_at TEXT NOT NULL);
chunks(id TEXT PRIMARY KEY, document_id TEXT NOT NULL, zone_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
       heading_path TEXT, page INTEGER, text TEXT NOT NULL, text_sha256 TEXT NOT NULL);
chunks_fts USING fts5(chunk_id UNINDEXED, text_bigram);
conversations(id TEXT PRIMARY KEY, zone_id TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL);
messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
         status TEXT NOT NULL, created_at TEXT NOT NULL);
citations(id TEXT PRIMARY KEY, message_id TEXT NOT NULL, chunk_id TEXT NOT NULL, document_id TEXT NOT NULL,
          page INTEGER, heading_path TEXT, snippet TEXT);
mindmaps(id TEXT PRIMARY KEY, zone_id TEXT NOT NULL, title TEXT NOT NULL, mode TEXT NOT NULL,
         created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
mindmap_nodes(id TEXT PRIMARY KEY, mindmap_id TEXT NOT NULL, node_key TEXT NOT NULL, parent_key TEXT,
              title TEXT NOT NULL, note TEXT, source_document_id TEXT, source_chunk_ids TEXT,
              origin TEXT NOT NULL, unverified INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
quizzes(id TEXT PRIMARY KEY, zone_id TEXT NOT NULL, mindmap_id TEXT, created_at TEXT NOT NULL);
quiz_questions(id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, idx INTEGER NOT NULL, type TEXT NOT NULL,
               stem TEXT NOT NULL, options TEXT, answer TEXT NOT NULL, explanation TEXT NOT NULL,
               citations TEXT NOT NULL, latex TEXT);
paper_jobs(id TEXT PRIMARY KEY, filename TEXT NOT NULL, original_path TEXT NOT NULL, sha256 TEXT NOT NULL,
           profile_digest TEXT, status TEXT NOT NULL, issues TEXT, operations TEXT, checks TEXT,
           corrected_docx TEXT, corrected_pdf TEXT, report_pdf TEXT, created_at TEXT NOT NULL);
usage_logs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, model TEXT,
           prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER, ok INTEGER NOT NULL,
           error TEXT, skill_name TEXT, skill_sha256 TEXT);
settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

- 枚举：`documents.status` = `parsing|ready|ready_with_warning|failed`；`messages.status` = `answered|partial|insufficient_evidence|error`；`paper_jobs.status` = `uploaded|checked|corrected|failed`；`mindmaps.mode` = `structure|concept`；`mindmap_nodes.origin` = `ai|user`。
- 时间统一 ISO8601 字符串；ID 用 `uuid4().hex`。
- 索引：`chunks(document_id)`、`chunks(zone_id)`、`messages(conversation_id)`、`citations(message_id)`、`documents(zone_id)`、`documents(sha256)`、`mindmap_nodes(mindmap_id)`。

## 6. HTTP API（路径/请求/响应字段固定；错误体 `{"error":{"code":"...","message":"..."}}`）

| # | 方法+路径 | 请求 | 响应 |
|---|---|---|---|
| 1 | GET `/api/health` | — | `{"ok":true,"model_configured":bool,"pdf_engine":"word|wps|none","fts5":true}` |
| 2 | GET `/api/settings` | — | `{"base_url","model","api_key_masked","pdf_engine","usage":[最近20条usage_logs]}` |
| 3 | PUT `/api/settings` | `{"base_url","api_key?"（空串=不修改）,"model"}` | `{"ok":true}` |
| 4 | POST `/api/settings/test` | `{}` | `{"ok":bool,"message"}`（真实发 1 次最小请求） |
| 5 | POST `/api/settings/pdf-test` | `{}` | `{"ok":bool,"engine":"word|wps|none","message"}` |
| 6 | GET `/api/zones` | — | `[{"id","name","doc_count","created_at"}]` |
| 7 | POST `/api/zones` | `{"name"}` | `{"id","name","created_at"}`（重名 409 `zone_duplicate`） |
| 8 | PATCH `/api/zones/{id}` | `{"name"}` | `{"id","name"}` |
| 9 | DELETE `/api/zones/{id}` | — | 204；有文档时 409 `zone_not_empty` |
| 10 | POST `/api/documents` | multipart `file`,`zone_id` | `{"document_id","status","warning"}`（同步解析完返回；同区同 sha256 重复→409 `duplicate_document`） |
| 11 | GET `/api/documents?zone_id=` | — | `[{"id","filename","ext","size","status","error","warning","chunk_count","page_count","created_at"}]` |
| 12 | POST `/api/documents/{id}/reindex` | — | `{"status","chunk_count"}` |
| 13 | DELETE `/api/documents/{id}` | — | 204（同删 chunks、fts、上传文件） |
| 14 | GET `/api/conversations?zone_id=` | — | `[{"id","zone_id","title","created_at","message_count"}]` |
| 15 | POST `/api/conversations` | `{"zone_id","title?"}` | `{"id","zone_id","title","created_at"}` |
| 16 | DELETE `/api/conversations/{id}` | — | 204（同删 messages/citations） |
| 17 | GET `/api/conversations/{id}/messages` | — | `[{"id","role","content","status","created_at","citations":[{"document_id","filename","page","heading_path","snippet"}]}]` |
| 18 | POST `/api/chat` | `{"conversation_id","question"}` | `{"message_id","status","answer","citations":[...],"usage":{"prompt_tokens","completion_tokens"},"retrieved":int}` |
| 19 | POST `/api/mindmaps` | `{"zone_id","document_ids":[...],"title","mode":"structure|concept"}` | `{"mindmap_id","mode","status","truncated":bool,"nodes":[...]}` |
| 20 | GET `/api/mindmaps?zone_id=` | — | `[{"id","zone_id","title","mode","updated_at","node_count"}]` |
| 21 | GET `/api/mindmaps/{id}` | — | `{"id","zone_id","title","mode","updated_at","nodes":[...]}` |
| 22 | PUT `/api/mindmaps/{id}` | `{"nodes":[...]}` | `{"ok":true,"updated_at"}` |
| 23 | POST `/api/mindmaps/{id}/regenerate` | `{}` | `{"mindmap_id","mode","nodes":[...],"kept_user_nodes":int,"removed_nodes":int}` |
| 24 | POST `/api/quizzes` | `{"zone_id","mindmap_id?"}` | `{"quiz_id","questions":[...]}`（固定 6 题） |
| 25 | GET `/api/quizzes/{id}` | — | `{"id","zone_id","created_at","questions":[...]}` |
| 26 | POST `/api/quizzes/{id}/export` | `{}` | `{"docx":"/exports/xxx.docx","pdf":"/exports/xxx.pdf"}` |
| 27 | POST `/api/papers` | multipart `file`（只收 .docx） | `{"paper_id","filename","sha256"}` |
| 28 | POST `/api/papers/{id}/check` | `{"profile_mode":"builtin|upload","profile_json"?}` | `{"status":"checked","profile_digest","issues":[{code,target,property,expected,actual,message}],"checked_count"}` |
| 29 | POST `/api/papers/{id}/execute` | `{}` | `{"status":"corrected","corrected_docx","corrected_pdf","report_pdf","checks":[...]}`；未检查/文件变更→409 `paper_version_mismatch` |
| 30 | GET `/api/usage?limit=50` | — | `[{"created_at","kind","model","prompt_tokens","completion_tokens","latency_ms","ok","error","skill_name"}]` |
| 31 | POST `/api/data/clear` | `{"confirm":"CLEAR"}` | `{"ok":true}`（真删业务数据+上传/导出文件，保留 config.json） |
| 32 | GET `/` 与 `/exports/*` | — | 静态页面与导出文件下载 |

- 所有响应 `Content-Type: application/json; charset=utf-8`（文件下载除外）。
- 不做分页；列表按创建时间倒序。

## 7. RAG 管线（参数固定）

**解析（按扩展名）**
- `.txt/.md`：UTF-8 读取，失败试 `gbk`，再失败 `document_parse_failed`
- `.pdf`：`PyMuPDF` 逐页 `page.get_text("text")`；若全文字符数 < 20 → 按扫描件处理：每页渲染 200dpi → rapidocr → 文本；`warning="OCR 已用于扫描页"`
- `.docx`：`python-docx` 按段落顺序取文本；标题段落用 style 名匹配 `Heading 1..3` 或 `标题 1..3` 记入 heading_path
- `.pptx`：逐页取文本框+备注；页标题记入 heading_path
- 图片 `.png/.jpg/.jpeg/.webp/.bmp`：rapidocr 单页 OCR
- 扫描 PDF OCR 最多处理前 30 页；超限 `status=ready_with_warning`，`warning="扫描件超过 30 页，仅索引前 30 页"`
- 单文件 ≤ 20MB，超限 413 `document_too_large`；不支持扩展名 415 `document_unsupported`
- 解析失败：`status=failed`，`error` 存实际异常摘要；不得显示"成功"

**切块**
- 先按标题/页边界切 section；section < 600 字与下一 section 合并（合并后 > 800 字则停止合并）；section > 800 字按 100 字 overlap 滑窗切
- 常量：`CHUNK_MIN=600`、`CHUNK_MAX=800`、`CHUNK_OVERLAP=100`
- 每 chunk 记 `heading_path`、起始 `page`、`text_sha256`

**索引（中文 bigram）**
- 处理函数：连续 CJK 字符段 → 相邻两字 bigram 用空格连接；非 CJK 保留并转小写
- 写入 `chunks_fts(chunk_id, text_bigram)`；建表语句 `CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, text_bigram)`

**检索**
- query 同样 bigram 化；FTS5 查询 = bigram 用 `OR` 连接；`WHERE` 限定 `chunks.zone_id = ?`
- 按 `bm25()` 排序取前 20；按 `text_sha256` 去重；取前 5 作为证据
- 上下文硬上限 4500 字符；超限截断最后一个 chunk 并追加 `[...]`
- 无结果 → 不调模型，直接返回 `insufficient_evidence`，文案固定："当前知识区没有找到能回答该问题的资料。"

**证据包（交给模型）**
```json
{"scope":{"workspace_id":"local","knowledge_base_id":"<zone_id>","conversation_id":"<id>","corpus_revision":"<该区文档数与最新创建时间的sha256前12位>"},
 "evidence":[{"evidence_id":"e1","document_id":"","document_version":"<doc.sha256>","chunk_id":"","content_hash":"<chunk.text_sha256>","locator":{"page":1,"section":"<heading_path>"},"text":"...","parse_status":"ready"}],
 "read_ledger":[],"budget":{"remaining_search_calls":0,"remaining_chunk_read_calls":0,"max_evidence_chunks":5,"max_evidence_tokens":3000}}
```

**模型调用**
- `POST {base_url}/chat/completions`；body：`model`、`temperature=0.2`、`max_tokens=1200`、`messages=[system,user]`
- system = 对应 skill 正文 + 证据包 JSON + 固定输出要求
- 超时 60s；网络错误或 5xx 重试 1 次（间隔 2s）；4xx 不重试
- **不使用 `response_format`**；要求模型只输出一个 JSON 对象；解析方式：取返回文本第一个 `{` 到最后一个 `}`，`json.loads`；失败则追加一次固定纠正消息重试；仍失败 → 502 `model_output_invalid`

**结果校验（固定）**
- `status` 必须属于 `answered|partial|insufficient_evidence|needs_parse|blocked_scope`
- `citations` 必须是本次 evidence_id 子集；非法 ID 丢弃并记 warning
- `answer` 非空
- 若 `status=answered` 但无有效引用 → 改为 `status=insufficient_evidence`，`answer="模型没有给出可核对的资料引用，本次回答不作为资料结论。"`
- 落库：user message + assistant message（含 status/citations）+ `usage_logs`（真实 token/latency/skill 名与哈希）
- 模型未配置 → 400 `model_not_configured`；调用失败 → 502 `model_request_failed`

**分块/检索参数一律使用 `config.py` 常量，禁止在调用处硬编码数字。**

## 8. 四个 Skill 的使用点（固定；不得新增/减少调用点）

总则：
1. 每次模型调用只加载"当前模块"的一个 skill 正文（剥离开头 `---` 到第二个 `---` 的 YAML），不得把多个 skill 或 references 全文并入同一请求；references 默认不发送给模型。
2. 加载后计算 skill 全文 sha256 前 12 位；每次调用写 `usage_logs`：`skill_name`、`skill_sha256`、`model`、`prompt_tokens`、`completion_tokens`、`latency_ms`、`ok`。
3. skill 正文放 system 消息；证据包/文档结构放固定 JSON 段；模型只输出一个 JSON 对象。
4. 禁止修改 `skills/` 下任何文件；禁止把 skill 正文复制进 `.py` 代码当常量。
5. skill 文件缺失 → 启动失败并打印缺失路径；模型未配置 → 对应接口 400 `model_not_configured`。

### 8.1 资料问答：`rag-evidence-reader`
- 唯一调用点：`POST /api/chat`
- 触发条件：检索到 ≥1 条证据；0 条证据时**不调模型**，直接 `insufficient_evidence`
- 输入：system = skill 正文 + 第 7 节证据包 JSON；user = 用户问题原文
- 输出校验：见第 7 节"结果校验"
- 失败：JSON 非法 → 附固定纠正消息重试 1 次 → 502 `model_output_invalid`；网络/5xx 重试 1 次 → 502 `model_request_failed`；4xx 不重试
- 记录：`kind="chat"`

### 8.2 出题：`grounded-quiz-generator`
- 唯一调用点：`POST /api/quizzes`
- 触发条件：证据检索到 ≥1 条；0 条 → 422 `insufficient_evidence`，不调模型
- 输入：system = skill 正文 + 证据包 JSON + 固定试卷约束 JSON（题量 6、题型顺序 `single_choice/multiple_choice/true_false/fill_blank/short_answer/calculation`、难度 `basic/basic/basic/advanced/advanced/comprehensive`、必须引用证据）；user = "按固定约束出 6 道题" + 导图考点线索（若传 `mindmap_id`）
- 输出校验：`status`/`requested_count=6`/`generated_count=6`；`questions` 长度=6 且题型顺序一致；每题 `type/stem/answer/explanation/citations` 必填；`citations` 均有效且非空；单选恰 1 个正确选项、多选 ≥2、判断题 answer ∈ {true,false}；`latex` 可为空但不得非法转义
- 失败：校验失败 → 附固定失败原因重试 1 次 → 502 `model_output_invalid`；资料不足 → 422 `insufficient_evidence`（固定文案："当前知识区的资料不足以生成 6 道题。"）
- 记录：`kind="quiz"`

### 8.3 论文：`paper-format-corrector`（只在"检查"阶段用模型）
- 调用点：`POST /api/papers/{id}/check`；`/execute` **不调用模型**
- 检查阶段两步：
  1. 程序确定性检查固定 11 项（第 12 节表格）→ `issues_program`
  2. 调模型（每份论文最多 1 次）：system = skill 正文 + 最终 profile JSON（不发送 `references/format-profile.md` 全文）；user = 结构化文档摘要（段落 id/前 80 字/样式名/是否标题/是否题注/节信息）+ `issues_program`
- 模型输出校验（按 skill 契约）：`status` ∈ `plan_ready|no_changes|partial|needs_profile|blocked_input`；`operations` 只接受 `apply_paragraph_style`/`normalize_run_font`/`set_section_layout` 三类，`target` 必须存在于输入，取值必须来自已选 profile；`issues` 每条必须有 `target_id/property/expected/reason`；任一非法项丢弃
- 合并规则：`issues = issues_program ∪ issues_model`（按 `target_id+property` 去重，程序优先）；`needs_review` 原样保留；`operations_model` 只作建议，不直接执行
- 模型不可用/未配置/输出非法：检查照常完成（只用 `issues_program`），报告注明"模型未参与角色判定"，不阻塞、不重试
- 记录：`kind="paper_role"`，失败也写 `ok=0`

### 8.4 导图：`mindmap-builder`（仅 M2 概念式）
- 唯一调用点：`POST /api/mindmaps`，且 `mode="concept"`
- `mode="structure"`（M1）**不调用模型、不加载 skill、不写 usage**，见第 10.2 节
- 触发条件：所选资料在当前知识区存在 ≥1 条 chunk；0 条 → 422 `insufficient_evidence`，不调模型
- 输入：system = skill 正文 + 第 10.3 节证据包 JSON；user = 标题 + `max_nodes=120` + `max_depth=5` + 生成要求
- 输出校验：`status` ∈ `ready|partial|insufficient_evidence|blocked_scope`；`nodes` 非空；`parent_id` 均存在且无环；非根节点 `refs` 映射为真实 chunk id，非法 refs 丢弃并把节点 `unverified=1`；节点数 >120 或深度 >5 → 按广度优先截断并返回 `truncated=true`；`ready` 要求节点数 ≥3 且全部非根节点有效 refs；`ready` 但非根节点全部无有效 refs → 降级 `insufficient_evidence`
- 失败：JSON 非法/结构非法 → 附固定纠正消息重试 1 次 → 502 `model_output_invalid`；**不自动降级到 M1**
- 记录：`kind="mindmap"`
- 禁止：M2 节点不得出现空 `source_chunk_ids` 却标记 `origin="ai"` 且 `unverified=0`；不得把用户已有节点直接当证据

## 9. 知识区与会话规则（固定）

- 知识区名 1–20 字符，去首尾空格，不得重名；删除前必须为空（无文档）
- 会话创建时必须选 1 个已存在知识区；此后不可换区；提问只检索该区
- 会话标题：首次提问时取该问题前 20 字；未提问前为 `新会话`
- 删除会话：级联删 messages/citations
- 全部数据单机单用户；不实现登录

## 10. 思维导图（双模式）

### 10.1 模式定义

| 模式 | 名称 | 是否调模型 | 资料限制 | 适用 |
|---|---|---|---|---|
| `structure` | M1 结构式 | 否 | DOCX/MD/PPTX（需标题层级） | 保真、零成本、零幻觉（默认） |
| `concept` | M2 概念式 | 是（`mindmap-builder`，1 次） | 任意格式（有 chunk 即可） | 需要提炼、重组逻辑时 |

### 10.2 M1 结构式（程序确定性生成）

- 只允许 DOCX/MD/PPTX；PDF/TXT/图片在资料选择列表置灰，固定提示："该格式没有可用的标题层级，不能生成结构导图"
- 树结构：虚拟根（`node_key="root"`，标题=用户输入标题，默认"资料结构导图"）→ 每份文档一个节点（`node_key=sha256(f"{zone_id}|{document_id}|")[:12]`）→ 章节节点（`node_key=sha256(f"{zone_id}|{document_id}|{heading_path}")[:12]`），层级按 heading 深度
- 节点字段：`title`=章节标题；`note`=该章节首个 chunk 前 60 字；`source_document_id`；`source_chunk_ids`=该章节全部 chunk id JSON 数组；`origin="ai"`；`unverified=0`

### 10.3 M2 概念式（调用 `mindmap-builder`）

**证据选择（固定）**
- 取所选资料在当前知识区的全部 chunk；每份文档按 `ordinal` 取前 12 个 chunk；总量 ≤40 个 chunk、总文本 ≤12000 字符；超出按文档轮询截断
- 证据包结构：`{"scope":{...},"evidence":[{"evidence_id":"e1","document_id":"","document_version":"<doc.sha256>","chunk_id":"","locator":{"page":1,"section":"<heading_path>"},"text":"...","parse_status":"ready"}],"max_nodes":120,"max_depth":5}`
- 0 条 chunk → 422 `insufficient_evidence`，固定文案："当前资料不足以生成导图。"

**调用与校验**
- 模型参数：`temperature=0.3`、`max_tokens=1500`、超时 60s、重试规则同第 7 节
- 校验规则见 8.4；节点 ID 由程序重写：`node_key=sha256(f"m2|{zone_id}|{parent_title_path}|{title}")[:12]`（根为 `root`；`parent_title_path` 为根到父节点的 `title` 以 `/` 连接）
- `refs` → `source_chunk_ids`；`source_document_id` 取第一个有效 ref 对应文档；多个来源时 `source_document_id` 记第一个
- 节点 `note` 直接使用模型输出；`origin="ai"`；非法 ref 的节点 `unverified=1` 且 `source_chunk_ids=[]`

### 10.4 通用规则（两种模式都适用）

- 编辑：改 title/note、增删节点（根不可删/改名）、保存（编辑过的节点 `origin="user"`）；节点上限 300，超限 400 `invalid_request`
- 溯源：节点"查看来源"打开其第一个 `source_chunk_ids` 对应 chunk 原文；`unverified=1` 的节点显示"未验证来源"
- 重新生成合并规则：按 `node_key` 匹配；`origin="user"` 的节点保留 title/note；新增 key 追加；消失 key 若 `origin="user"` 保留且 note 追加 `"[来源已变更]"`，否则删除；返回 `kept_user_nodes`/`removed_nodes`
- 不导出图片/Markdown/OPML
- 前端 map 页提供两个按钮：`结构式生成` 与 `AI 生成（概念导图）`；M2 失败固定提示："AI 导图生成失败：{错误信息}。可改用结构式生成。"（不自动降级）

## 11. 出题成卷（固定 6 题，学生版导出）

- 请求必须带 `zone_id`；`mindmap_id` 可选（可为 M1 或 M2 导图）
- 固定试卷：`idx 1..6`，题型顺序 = `single_choice, multiple_choice, true_false, fill_blank, short_answer, calculation`；难度依次 = `basic, basic, basic, advanced, advanced, comprehensive`
- 若传 `mindmap_id`：取该导图全部节点 title 拼接为"考点线索"并入 prompt；命题仍必须引用证据，`unverified=1` 或 `source_chunk_ids=[]` 的节点不得作为依据
- 证据：对该区按"导图节点标题拼接（无导图则用该区名称）"检索 top 5 个 chunk；证据不足时返回 422 `insufficient_evidence`，文案："当前知识区的资料不足以生成 6 道题。"
- 模型输出按 quiz skill 契约；校验见 8.2
- 页面展示：题号/题型/题干/选项/答案/解析/来源（教师信息仅页面）
- 导出学生版 DOCX：标题 `《{zone_name}》练习卷（学生版）`；含题号、题干、选项、作答区；**不含答案/解析/来源**；每页页脚居中页码；公式转 OMML
- 文件：`data/exports/{quiz_id}_student.docx`、`data/exports/{quiz_id}_student.pdf`
- **不导出教师版**；不生成答案页

## 12. 论文格式检查与修正（DOCX）

**输入**：仅 `.docx` ≤20MB；原稿保存在 `data/uploads/`，永不覆盖。

**模板**
- 内置：`skills/paper-format-corrector/assets/generic-cn-course-paper-v1.json`
- 上传覆盖：JSON 结构与内置一致，允许部分字段；校验 `profile_id`(string)、`page`(object)、`styles`(object)；未知顶层键忽略；合并后 `profile_digest = sha256(canonical_json)[:16]`
- 模板冲突/缺失字段：沿内置值；`profile_mode="upload"` 但 JSON 非法 → 400 `invalid_request`

**检查项（固定 11 条，code 与判定如下）**

| code | 检查内容 |
|---|---|
| `TITLE_STYLE` | 首个非空段落：字号/字体/加粗/居中 与 `profile.styles.title` 一致 |
| `ABSTRACT_STYLE` | 以"摘要"开头的段落：字体/字号/行距 与 `body` 一致 |
| `KEYWORDS_COUNT` | 以"关键词"开头的段落：关键词 3–5 个（团队默认，非校规） |
| `HEADING_STYLE` | 一/二/三级标题段落：字体/字号/加粗/对齐 与 `heading1..3` 一致 |
| `BODY_STYLE` | 正文段落：字体/字号/行距/首行 2 字符/两端对齐 与 `body` 一致 |
| `PAGE_LAYOUT` | 节纸张 A4、页边距与 `profile.page.margins_mm` 一致 |
| `CAPTION_STYLE` | 以"图 "或"表 "开头并带编号的段落：字体/字号/居中 与 `caption` 一致 |
| `FOOTER_PAGENUM` | 页脚是否含 `PAGE` 域 |
| `TOC_FIELD` | 存在"目录"标题段落但其后是否含 `TOC` 域 |
| `REFERENCES_ORDER` | 参考文献条目是否以 `[n]` 递增连续；正文中 `[n]` 引用编号必须 ≤ 条目数 |
| `WHITESPACE` | 行尾空格、连续 ≥3 换行 |

- 检查结果写 `paper_jobs.issues`；检查阶段不得修改文件。

**执行修正（用户确认后调用）**
- 操作白名单（仅 6 种）：`apply_paragraph_style`、`normalize_run_font`、`set_section_layout`、`insert_page_number_field`、`replace_toc_field`、`cleanup_whitespace`
- 禁止：删除/重排正文、改文字、改公式/图片/表格/脚注/书签/超链接、重建整篇纯文本
- 目录：若存在"目录"标题段落，把其后的目录正文替换为 `TOC \o "1-3" \h \z \u` 域；报告注明"需在 Word/WPS 中按 F9 更新目录"
- 页码：每个 section 页脚清空后居中插入 `PAGE` 域（宋体 10.5pt）
- 页眉：保持原样，不修改；报告中注明"模板未定义页眉内容"
- 参考文献：只检查、不修改
- 输出：`data/exports/{paper_id}_corrected.docx`、`{paper_id}_corrected.pdf`、`{paper_id}_report.pdf`；原稿保留

**执行后校验（全部通过才允许下载）**
1. 重新打开修正版：`python-docx` 读取段落文本序列（去空白）与原稿一致
2. 图片/表格/内联公式对象数量与原稿一致
3. 节数与原稿一致
4. 校验失败 → `status=failed`，不提供下载，错误写入 `checks`

**检查报告 PDF（固定字段）**：原文件名、sha256、模板来源、profile_digest、检查时间、问题清单（code/位置/期望/实际/说明）、已执行操作、校验结果、已知限制。

## 13. PDF 转换（固定顺序）

1. 优先 Word COM：`win32com.client.Dispatch("Word.Application")`；`Visible=False`；`doc=Word.Documents.Open(abs_path, ReadOnly=True)`；先 `doc.ExportAsFixedFormat(abs_pdf, 17)`；异常再试 `doc.SaveAs(abs_pdf, FileFormat=17)`；最后关闭文档与 Word
2. Word COM Dispatch 失败 → WPS COM：`Dispatch("KWps.Application")`，同上流程
3. 两者都失败 → 500 `pdf_engine_unavailable`；前端文案固定："PDF 转换不可用：请确认已安装 Microsoft Word 或 WPS，并在设置中运行转换自检。"
4. 设置页"PDF 自检"：生成一份临时 DOCX（一行测试文字）→ 转 PDF → 返回 `{"ok","engine","message"}`；临时文件放 `data/tmp/` 并清理
5. 转换失败必须关闭文档与进程，不得遗留锁文件

## 14. 前端（单文件 web/index.html）

- 无框架、无 CDN、无外部字体/图片；沿用现有绿色/米色 CSS 变量；桌面 ≥1280px；不做移动端
- 路由（前端 `view` 状态）：`chat`（默认）| `library` | `map` | `assignment` | `settings`
- **布局固定**：
  - 顶部栏：左"知序"品牌 + 当前知识区名；右"模型：已配置/未配置" + "设置"
  - 左侧栏 260px：`+ 新建会话`；会话列表；分隔线；固定入口 `知识库` `思维导图` `作业`
  - chat：顶部"只检索：{知识区名}"；消息区；右侧引用抽屉（360px，可折叠）；底部输入区（`上传资料`附件按钮 + 输入框 + `发送`）
  - library：知识区列表 + `新建知识区`；文档表格（列：文件名/类型/大小/状态/片段数/上传时间/操作）；`上传资料`按钮；操作含"重建索引""删除"
  - map：导图列表；`结构式生成`与`AI 生成（概念导图）`按钮；画布（节点可点选）；右侧编辑面板（标题/说明/来源/保存）；`重新生成（保留手改）`按钮
  - assignment：两个 tab `论文格式修正` / `出题成卷`
  - settings：base_url / api_key / model 输入 + `保存` + `测试连通` + `PDF 转换自检` + 用量表（最近 20 条：时间/类型/模型/输入token/输出token/耗时/结果）+ `清空所有数据`（二次确认输入 `CLEAR`）
- **文案固定（不得改写）**：
  - 无知识区："还没有知识区。先新建一个知识区，再上传资料。"
  - 无会话："在左侧新建会话，提问只检索当前知识区。"
  - 无资料："这个知识区还没有资料。点击右上角上传资料。"
  - 未配置模型："未配置模型 API：请到设置填写 base_url、api_key、model。"
  - 资料不足："当前知识区没有找到能回答该问题的资料。"
  - AI 导图失败："AI 导图生成失败：{错误信息}。可改用结构式生成。"
  - 生成中："正在检索资料…" / "正在生成回答…" / "解析中…"
  - 失败统一："操作失败：{错误信息}"
  - 删除确认："删除后不可恢复，确定删除？"
- 前端不保存 API Key、不把业务数据写 localStorage；localStorage 只允许存 `{view, drawerOpen}`
- 任何页面/弹窗不得出现：总览、任务、打卡、统计图表、最近学习、示例数据

## 15. 数据真实性（红线，违反即失败）

1. 首次启动 0 数据：无 zone/doc/conversation/message/mindmap/quiz/paper/usage
2. 所有计数 = SQL COUNT；无数据 → 0 或空状态；不使用占位假数据
3. 不实现演示模式；不写死任何示例资料/题库/问答/计划/统计
4. 模型未配置或调用失败 → 显示错误，不返回假回答
5. 引用必须来自 `citations` 表且指向真实 chunk；无引用不得声称有依据
6. 导图节点必须来自真实资料：`origin="ai"` 的节点必须有有效 `source_chunk_ids`；`unverified=1` 必须在界面标注
7. `scripts/smoke.py` 的测试数据只能写入临时 `DATA_DIR`，不得进入正式界面

## 16. 错误码（固定）

`invalid_request`(400)、`zone_duplicate`(409)、`zone_not_found`(404)、`zone_not_empty`(409)、`document_unsupported`(415)、`document_too_large`(413)、`document_parse_failed`(422)、`duplicate_document`(409)、`conversation_not_found`(404)、`model_not_configured`(400)、`model_request_failed`(502)、`model_output_invalid`(502)、`insufficient_evidence`(422)、`pdf_engine_unavailable`(500)、`paper_version_mismatch`(409)、`paper_check_required`(409)、`export_not_ready`(409)、`internal_error`(500)。

## 17. 安全

- 密钥只存 `data/config.json`；接口只回掩码；日志不得输出 key、资料全文、完整 prompt
- 上传文件名用 uuid 落盘，原文件名只存 DB；路径不得拼接用户输入
- 不提交 key、真实资料、导出文件；`.gitignore` 见第 4 节

## 18. 实施顺序（必须按序；每阶段通过自测与对应验收项后才能进入下一阶段）

| 阶段 | 内容 | 通过条件 |
|---|---|---|
| 1 | 目录/依赖/config/db 空库、健康检查、设置页（含 PDF 自检） | `/api/health` OK；PDF 自检可用；空库 0 数据 |
| 2 | 六种格式解析（含 OCR）、切块、FTS5、文档状态、知识库页 | K1–K5 通过 |
| 3 | 会话/消息/检索/模型调用/skill 加载/引用/资料不足/用量 | R1–R8、C1–C4 通过 |
| 4 | M1 结构式导图 → M2 概念式导图（含 skill 调用）→ 固定 6 题/页面/学生版 DOCX+PDF | M1–M8、Q1–Q5 通过 |
| 5 | 论文检查/执行/校验/修正版+报告+PDF | P1–P8 通过 |

- 每阶段结束：运行 `scripts/smoke.py`（该阶段范围）+ 手动执行 `docs/acceptance.md` 对应项；向用户报告结果后再继续。
- 任一阶段验收失败：修复后重跑；不得跳过。

## 19. 验收清单（全部必须通过）

**数据真实性**
- D1 全新启动（清空 `data/`）：知识区/资料/会话/消息/导图/试卷/论文/用量全部为空
- D2 只上传 1 份资料后：资料数=1，片段数=真实解析数，其他模块仍为空
- D3 不点击"生成"时，界面不出现任何导图/试卷/论文方案
- D4 未配置模型或断网提问：显示真实错误，无假回答、无假引用
- D5 全仓库搜索：无 `DEMO_QA`、无写死的示例资料/题库/问答/任务/统计

**RAG / 知识库**
- R1 建 2 个知识区各传 1 份资料，分别提问，引用只来自对应区
- R2 只有 A 区能回答的问题在 B 区提问 → 资料不足且无引用
- R3 点击引用能打开原文，片段与 chunk 文本一致
- R4 重启服务后数据、消息、引用仍在
- R5 未配置模型时提问 → 固定文案提示
- R6 模型返回非法引用 → 非法项被丢弃，无有效引用时降级为资料不足
- R7 `usage_logs` 记录真实 token 与延迟，设置页可见
- K1 知识区增删改、重名拒绝、有文档时删除被拒
- K2 六种格式各上传 1 份，状态 `ready` 或 `ready_with_warning`
- K3 重建索引后片段数 > 0；删除文档后检索不到
- K4 上传超过 20MB、错误扩展名 → 明确错误
- K5 OCR 扫描件能出文本并参与检索

**聊天/导图/出题/论文**
- C1 新建会话必须选知识区；创建后不可换区；两个会话历史互不干扰
- M1 结构式导图节点与标题层级一致
- M2 概念式导图节点 ≥3，且每个非根节点可溯源到真实 chunk
- M3 编辑保存后刷新仍在
- M4 M1 重新生成保留手改节点
- M5 M2 重新生成保留手改节点，且不出现重复节点
- M6 M2 资料不足 → 422 且不生成占位节点
- M7 M2 模型未配置/失败 → 固定错误提示，不自动降级、不生成占位节点
- M8 构造超限资料 M2 生成 → 节点 ≤120、深度 ≤5、返回 `truncated=true`
- Q1 固定 6 题且题型顺序固定；Q2 每题有有效引用；Q3 学生版 DOCX 无答案/解析/来源；Q4 PDF 可打开；Q5 页面显示答案与来源但无教师版导出
- P1 内置模板检查出问题清单；P2 上传覆盖模板后 digest 变化且检查按覆盖值执行；P3 执行修正后原稿保留、修正版与报告可下载；P4 修正版段落文本/对象/节数校验通过；P5 目录域存在且报告提示 F9 更新；P6 页码域存在；P7 参考文献编号问题能检出；P8 修正版与报告 PDF 可打开

**工程**
- X1 `README` 按步骤 3 分钟内可跑通；`start.bat` 一条命令可启动
- X2 仓库不含 `api_key`、`data/`、真实资料
- X3 规格中的接口/表/文案/参数未被改动（自查并报告）
- X4 `usage_logs` 中每次模型调用都带 `skill_name` 与 `skill_sha256`（chat/quiz/paper_role/mindmap 四类齐全）

## 20. 完成后回复格式（固定）

```text
任务/阶段：
本次完成：
实际改动文件：
新增接口与数据结构：无 / 与规格一致（列出偏差）
验证命令与结果（对应第 19 节编号逐条）：
未完成项及原因：
已知限制：
下一位需要同步的事项：
```

## 21. 红线（违反任一即视为任务失败）

1. 增加任何范围外功能、页面、依赖、数据表、接口
2. 任何种子/演示/示例数据进入正式界面，或保留总览/任务/统计类假数据
3. 用 mock 冒充模型调用、检索或引用
4. 更换技术栈/数据库/目录结构
5. 伪造验收结果或跳过验收
6. 把 api_key、真实资料、导出文件写进仓库
7. 自行修改规格中的文件名、表名、接口字段、状态枚举、界面文案、数值参数
8. M2 导图节点伪造 refs，或把 `unverified=1` 节点当作资料依据
