# 学习复习工作台 · 架构说明文档

> 版本：v0.2（评审中，Q1/5 已确认）
> 日期：2026-11-17
> 状态：评审中 · 决策记录见 §15

**已确认基线（Q1）**：本地单机自用（Docker Compose）· 浏览器 Web 首发、后期 Tauri 打包 · 全部模型走云端 API，不做本地大模型部署。
这三条决定了：**无多租户、无账号体系、无 GPU 依赖、无 K8s**；安全重点从"租户隔离"转为"本机数据不误外发"。

---

## 1. 文档目的与阅读指引

本文档描述"个人/小团队学习复习工作台"（下称"平台"）的总体架构、模块划分、数据模型、关键流程与技术选型。

初版基于一句话愿景推导而来，必然存在大量假设。文中凡标注 `【待确认 Qn】` 的条目，均为后续 5 轮问答需要澄清的假设点；问答完成后，这些标记会被替换为已确认的决策记录（含理由）。

假设清单集中见 §12，你可以在评审时直接对照该表。

---

## 2. 产品愿景与核心场景

### 2.1 一句话定义

> 把"我手上的所有资料"变成"可提问、可推导图、可顺着关键词探索"的知识体。

平台不是又一个通用聊天机器人，而是一个**以你的资料为唯一事实来源（grounded）的复习工作台**：所有回答必须能回溯到你上传的原文片段。

### 2.2 三个核心能力（对应原始预想）

| 编号 | 能力 | 用户价值 |
|---|---|---|
| C1 | **多格式资料接入** | 任何形式的文件（PDF/Word/PPT/图片/Markdown/代码/音视频/网页）拖进来即可用 |
| C2 | **基于资料问答（RAG）** | 接自己的 AI API，回答严格引用你的文档，带原文出处 |
| C3 | **知识可视化与探索** | 自动生成思维导图；生成 Obsidian 风格的关系图谱/关键词云，点关键词直达知识点 |

### 2.3 典型用户旅程

```
拖入资料 → 自动解析与索引（后台）→
  ├─ 路径 A：提问 → 检索命中的片段 → 流式回答 + 引用角标 → 点击角标跳回原文
  ├─ 路径 B：选一片资料/一个主题 → 生成思维导图 → 前端编辑/裁剪 → 导出
  └─ 路径 C：打开全局图谱 → 点击"熵增定律"这个关键词 →
              看到它在哪些文档里出现、和哪些概念相连 → 一键"围绕它出题/生成子导图/提问"
```

三条路径共享同一套**中间表示（知识单元 + 引用关系）**，这是架构的关键取舍：解析一次，四处复用。

---

## 3. 需求清单

### 3.1 功能需求（Functional）

**FR-1 资料接入**
- FR-1.1 支持拖拽/批量上传、文件夹上传、URL 抓取（可选）
- FR-1.2 支持格式见 §5.1，含扫描件 OCR、含音视频转写
- FR-1.3 重复文件去重（内容哈希）、版本替换
- FR-1.4 解析状态可见（排队/解析中/成功/失败+原因）
- FR-1.5 资料可归类到"工作区/科目/章节"（组织维度）

**FR-2 问答**
- FR-2.1 对话式问答，支持多轮上下文
- FR-2.2 检索范围可切换：单文档 / 指定多文档 / 整个工作区
- FR-2.3 回答必须带引用（文档名 + 页码/章节 + 片段预览）
- FR-2.4 无可靠依据时明确说"资料中未找到"，不臆造
- FR-2.5 支持追问、要求"只讲第 3 章""用更简单的语言重讲"
- FR-2.6（可选）生成复习题/闪卡，答案同样可溯源

**FR-3 思维导图**
- FR-3.1 以文档/章节/选中片段/某个关键词为中心生成导图
- FR-3.2 节点可展开/折叠、可编辑文字、可增删、可拖动布局
- FR-3.3 节点可回链原文片段（点击溯源）
- FR-3.4 导出 PNG / SVG / Markdown（兼容 XMind/MindNode 的 OPML/Markdown 大纲）

**FR-4 关键词云与关系图谱**
- FR-4.1 自动抽取关键词，按重要度渲染词云（字号/颜色映射权重与聚类）
- FR-4.2 关系图谱（力导向）：节点=概念/文档/章节，边=共现/语义相似/AI 抽取的明确关系
- FR-4.3 点击关键词 → 知识卡片（定义摘要 + 出处列表 + 相关概念 + 相关问答历史）
- FR-4.4 从关键词直接发起：围绕它提问 / 生成子导图 / 生成题目
- FR-4.5 图谱可按工作区、按文档、按时间范围过滤

**FR-5 系统能力**
- FR-5.1 可配置多个 AI Provider 与模型（API Key 自填、可测连通性）
- FR-5.2 用量与成本统计（每会话/每日 token 与费用）
- FR-5.3 数据可导出、可彻底删除（GDPR 式"遗忘"）

### 3.2 非功能需求（NFR）

| 编号 | 维度 | 目标（初版假设，待确认） |
|---|---|---|
| NFR-1 | 性能 | 首字延迟 ≤ 3s（P50）；单文档 200 页解析 ≤ 5min；图谱首屏 ≤ 2s（≤5k 节点） |
| NFR-2 | 规模 | **单用户单机**：资料 ≤ 20GB / ≤ 5 万文档；并发仅 1~2 人（Q1 已确认，无多租户） |
| NFR-3 | 可用性 | 单机部署；解析失败可重试、可局部降级；不许因队列阻塞导致 API 不可用 |
| NFR-4 | 成本 | 单次问答平均 ≤ ¥0.05（靠缓存 + 模型分级 + 重排裁剪） |
| NFR-5 | 隐私 | 单机自持数据；**外发边界可视化**（界面明示"本次请求将发送给哪个厂商"），支持一键全局断网模式（Q1 调整：不做本地大模型，故"零外发"= 禁用所有云端调用并降级为纯本地检索/关键词） |
| NFR-6 | 可维护 | 模块解耦，AI Provider / 解析器 / 渲染器均为可插拔 |

---

## 4. 总体架构

### 4.1 分层视图

```
┌──────────────────────────────────────────────────────────────────────┐
│  客户端层（Web SPA，未来可加 Desktop/移动）                            │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────────────┐   │
│  │ 资料库    │ 对话/问答 │ 思维导图  │ 词云/图谱 │ 设置(模型/密钥)  │   │
│  └──────────┴──────────┴──────────┴──────────┴──────────────────┘   │
│  渲染引擎：markmap / React Flow · Cytoscape.js · d3-cloud            │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ HTTPS · REST + SSE(流式) + WebSocket(任务进度)
┌───────────────────────────▼──────────────────────────────────────────┐
│  接入层（API Gateway / BFF）                                          │
│  鉴权(JWT) · 限流 · 请求校验 · SSE 转发 · 审计日志                      │
└───────────────────────────┬──────────────────────────────────────────┘
        ┌───────────────────┼────────────────────┬───────────────────┐
        ▼                   ▼                    ▼                   ▼
┌───────────────┐  ┌─────────────────┐  ┌────────────────┐  ┌──────────────┐
│ 资料服务        │  │ 检索问答服务      │  │ 知识构建服务     │  │ AI 网关       │
│ Ingest        │  │ Retrieval & QA  │  │ Knowledge      │  │ LLM Gateway  │
│ ·上传/去重/校验  │  │ ·混合检索        │  │ ·导图生成        │  │ ·Provider 抽象│
│ ·任务编排        │  │ ·上下文组装      │  │ ·关键词抽取      │  │ ·路由/降级    │
│ ·组织/版本       │  │ ·引用生成        │  │ ·实体关系抽取    │  │ ·流式/Schema  │
│               │  │ ·对话记忆        │  │ ·聚类/布局参数   │  │ ·用量计量     │
└───────┬───────┘  └────────┬────────┘  └───────┬────────┘  └──────┬───────┘
        │                   │                   │                  │
┌───────▼───────────────────▼───────────────────▼──────────────────▼───────┐
│  异步任务层（Worker Pool + 队列）                                          │
│  解析 · OCR · 转写 · 分块 · 嵌入 · 抽取 · 聚类 · 重建索引 · 缓存预热        │
└───────────────────────────┬──────────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  数据层                                                                    │
│  对象存储(原件/预览/切片图) · PostgreSQL(元数据+向量+全文) ·                │
│  图索引(邻接表，可外挂 Neo4j) · Redis(队列/会话/语义缓存)                     │
└──────────────────────────────────────────────────────────────────────────┘
                            │
                    ┌───────▼────────┐
                    │ 外部 AI 服务     │
                    │ OpenAI 兼容端点  │
                    │ Anthropic/Gemini │
                    │ 本地 vLLM/Ollama │
                    └────────────────┘
```

### 4.2 架构关键取舍（ADR 摘要）

| ADR | 决策 | 理由 | 代价 |
|---|---|---|---|
| ADR-1 | **解析一次，产出统一"知识单元（Chunk）"** 供问答/导图/图谱共用 | 三种能力天然共享同一事实来源，避免三套索引漂移 | Chunk 结构要预留足够元数据（层级、页码、bbox） |
| ADR-2 | **重解析与重索引全异步**，前端只订阅任务进度 | 大文件解析耗时不可控，同步会拖垮网关 | 需要任务状态机与幂等设计 |
| ADR-3 | **AI 能力统一走 Gateway**，业务层只依赖抽象接口 | 换模型/加供应商不改业务代码；便于成本与限流统一收口 | 抽象层要覆盖流式、JSON Schema、工具调用差异 |
| ADR-4 | **图谱先用 PostgreSQL 邻接表 + 应用层图算法** | 个人规模（万级节点）无需 Neo4j，少一个运维组件 | 复杂图查询（多跳）需手写递归 CTE |
| ADR-5 | **导图/图谱均为"AI 生成结构 + 人工编辑"双轨** | 纯自动结果不可靠，复习场景需要用户校正与裁剪 | 需持久化编辑态与"重新生成时保留手改"的合并策略 |
| ADR-6 | **所有生成结果携带 provenance（来源 chunk id 列表）** | 复习场景的可信度是刚需，也是差异化 | 增加存储与 prompt 设计复杂度 |

---

## 5. 模块详细设计

### 5.1 资料接入与解析（Ingest & Parsing）

#### 5.1.1 格式矩阵（初版，待 Q2 收敛）

| 类别 | 格式 | 解析策略 | 备注 |
|---|---|---|---|
| 文档 | PDF（文本层） | PyMuPDF 抽取文本 + 版面块（标题/段落/表格/图注），保留页码与 bbox | 首选路径，质量最高 |
| 文档 | PDF（扫描件） | 渲染为图 → OCR（PaddleOCR/Tesseract）→ 版面重建 | 走异步 OCR 队列，成本高 |
| 文档 | DOCX / ODT | python-docx / mammoth，按 Heading 样式还原层级 | 层级对导图极有价值 |
| 演示 | PPTX / KEY | python-pptx，按 slide 切块，提取备注 | 每 slide 一个逻辑单元 |
| 表格 | XLSX / CSV | pandas 读取，转为"表头+行"的自然语言化片段 | 需保留列语义，避免丢上下文 |
| 纯文本 | MD / TXT / HTML / EPUB | pandoc / BeautifulSoup / ebooklib，MD 直接读 AST | MD 可作为"原生结构"最佳来源 |
| 代码 | .py/.js/.java/... | tree-sitter 按函数/类切块，保留文件路径与符号名 | 面向计算机类复习 |
| 图片 | PNG/JPG/WEBP | OCR + 可选多模态模型描述（图表/公式） | 公式建议接 LaTeX-OCR |
| 音视频 | MP3/WAV/MP4 | Whisper 转写 + 分段带时间戳 | 长音频需分片并行 |
| 网页 | URL | Readability 抽取正文 → 转 MD | 可选功能 |

#### 5.1.2 处理管线

```
上传 → 前置校验（大小/白名单/MIME 嗅探/哈希去重/病毒扫描）
     → 落对象存储（原件不可变）
     → 建 document 记录（status=queued）
     → 入队 parse_job
        Worker: 提取 → 规范化（Unified Doc IR）→ 结构化切块 → 元数据打标
     → 入队 embed_job → 向量化 → 写 chunks.embedding + 全文索引
     → 入队 enrich_job（可选）→ 关键词/实体/关系抽取（供图谱）
     → status=ready（失败则 failed + 错误码，可重试/换解析器重试）
```

**统一文档中间表示（Unified Doc IR）**——这是所有下游能力的地基：

```jsonc
{
  "document_id": "doc_01H...",
  "title": "第五章 热力学第二定律",
  "source": {"mime": "application/pdf", "pages": 42, "lang": "zh"},
  "outline": [                      // 结构树，导图的直接来源
    {"level": 1, "text": "5.1 熵与熵增", "page": 12,
     "blocks": ["blk_101", "blk_102"]}
  ],
  "blocks": [                       // 版面块
    {"id": "blk_101", "type": "paragraph|heading|table|figure|caption|formula|code",
     "text": "...", "page": 12, "bbox": [x0,y0,x1,y1],
     "style": {"bold": false, "size": 10.5}, "order": 1}
  ],
  "assets": [{"id": "img_7", "kind": "figure", "ocr_text": "...", "caption": "..."}]
}
```

**切块策略（结构感知 + 语义兜底）**：
1. 优先按 `outline` 层级切分（章节 → 小节），保证每块语义自洽；
2. 单块超过 `max_tokens`（默认 800，待 Q3 调参）时按段落滑窗切分，overlap 15%；
3. 每块继承祖先标题链作为 `heading_path`（如 `第5章 > 5.1 熵 > 熵增原理`），
   并在嵌入前拼到正文前缀——这对"章节级提问"的召回提升明显；
4. 表格转为 markdown 表格文本，并保留列名；
5. 公式单独成 block，转 LaTeX，正文保留占位符。

### 5.2 检索与问答（Retrieval & QA）

#### 5.2.1 检索管线（混合检索 + 重排）

```
Query
 ├─ 查询理解：口语化改写、术语归一、指代消解（多轮时）
 ├─ 路由：范围过滤（workspace/doc_ids/时间）+ 是否需要跨语言
 ├─ 并行召回
 │   ├─ 稠密检索：pgvector HNSW，top-k=40
 │   ├─ 稀疏检索：PG 全文/BM25（中文用 pg_jieba 或 zhparser），top-k=40
 │   └─（可选）图谱检索：命中关键词 → 扩一跳邻居的 chunk
 ├─ 融合：RRF（Reciprocal Rank Fusion）
 ├─ 重排：bge-reranker / Cohere Rerank，取 top-8（可按预算跳过）
 ├─ 上下文组装：按 heading_path 聚合相邻块，去冗余，编号 [1][2]...
 └─ 生成：System(角色+禁止臆造+引用格式) + Context(带编号) + History + Question
```

#### 5.2.2 引用（Citation）机制

- 上下文块以 `[1] 文档《X》p.12 §5.1 (chunk_id)` 形式注入；
- 要求模型在句末输出 `[[1]]` 形式标记（或用结构化输出后处理对齐）；
- 服务端校验标记合法性（不存在的编号丢弃），转换为 `citation` 记录；
- 前端渲染角标 → 侧栏展示原文片段 + 高亮 bbox（PDF 可定位到页）。

#### 5.2.3 对话记忆

- 短期：最近 N 轮（默认 6 轮）原始消息；
- 长期：超出窗口时用"滚动摘要"压缩早期对话（小模型执行，成本低）；
- 会话与资料范围绑定，切换工作区时上下文隔离。

#### 5.2.4 不做的事（初版边界）

- 不做自主多步 Agent 规划（避免不可控成本与延迟）；
- 不做联网搜索（除非用户显式开启，且会标注"非资料内"）。

### 5.3 思维导图（Mind Map）

#### 5.3.1 两种生成模式

| 模式 | 输入 | 生成方式 | 适用 |
|---|---|---|---|
| M1 结构式（保真） | 文档 outline | **不调用 LLM**，直接把 IR 的标题树转为导图 | 教材/规范文档，零成本、零幻觉 |
| M2 概念式（提炼） | 选中片段/章节/关键词 | LLM 生成结构化 JSON 树（Schema 约束） | 需要"提炼要点""重组逻辑"时 |

推荐默认 M1，用户可对任一节点"AI 展开"（局部按需调用 M2）——这是控制成本与幻觉的核心策略。

#### 5.3.2 生成契约（结构化输出）

```jsonc
{
  "title": "热力学第二定律",
  "nodes": [
    {"id": "n1", "parent": null, "text": "熵", "kind": "concept",
     "summary": "系统无序度的度量", "refs": ["chunk_881"], "collapsed": false}
  ],
  "meta": {"model": "...", "prompt_version": "mm_v3", "source_scope": ["doc_01H..."]}
}
```

- 用 JSON Schema（严格模式）约束输出，失败自动重试 1 次并降级为"文本大纲→树"解析；
- 节点上限（默认单次 ≤ 120 节点）与深度上限（默认 5 层）防止爆树；
- 校验：`refs` 必须存在于本次提供的 chunk 集合内，否则置空并标记 `unverified`。

#### 5.3.3 前端与编辑态

- 渲染：`markmap`（大纲式、代码量小）或 `React Flow / AntV G6`（自由布局、可拖拽）；
  【待确认 Q4】默认视图与是否要求双模式
- 编辑模型：**"生成版"与"用户版"分离**——重新生成只覆盖生成版，用户的手动修改以 diff 形式叠加（保留 `origin: ai|user` 字段）；
- 导出：SVG/PNG（前端）、Markdown 大纲、OPML（导入 XMind）。

### 5.4 关键词云与关系图谱（Graph & Cloud）

这是"Obsidian 图谱体验"的核心，拆成三个子能力：

#### 5.4.1 关键词抽取（无需 LLM 的基线）

- 中文分词（jieba/pkuseg）+ TF-IDF / TextRank（YAKE 兜底）→ 候选词；
- 用**跨文档频次 × 章节分布**修正权重（只在 1 篇出现的词降权，避免专有噪声）；
- 过滤停用词 + 领域词表（可维护自定义词表，含同义词归并）；
- 成本：纯本地，零 token。**LLM 只用于给关键词生成"一句话定义"，且按需/批量缓存。**

#### 5.4.2 关系边构建（三级来源，可控可解释）

| 边类型 | 来源 | 权重计算 | 说明 |
|---|---|---|---|
| `cooccur` 共现 | 同一 chunk / 同一段落内共现 | PMI 或 归一化次数 | 零成本，覆盖广 |
| `similar` 语义近似 | 关键词向量/所属 chunk 向量余弦 | 阈值截断（如 >0.82） | 用 embedding 复用，零额外成本 |
| `explicit` 显式关系 | LLM 抽取（选修）| 置信度 | 带关系名（"导致/属于/对比"），质量最高、有成本 |

初版默认只用前两类，第三类由用户对选中区域手动触发"增强分析"，避免全库抽取的巨额开销。

#### 5.4.3 图结构与应用层算法

- 节点：`concept`（关键词）、`document`、`section`（章节），可选 `question`（问过的题）；
- 边：见上表 + `mentions`（文档→概念）；
- 存储：`graph_nodes` / `graph_edges` 表（邻接表），常用查询建索引；
- 应用层算法：度中心性（节点大小）、Louvain/标签传播（社区着色，本地跑，节点数 <1e4 秒级）、PageRank（可选）；
- 前端渲染：`Cytoscape.js`（WebGL 版 `Sigma.js` 用于 >5k 节点），力导向参数（斥力/边长/引力）暴露给用户；
- 大图降级：超过阈值时按"重要度 top-N + 当前关键词两跳邻域"裁剪，避免卡顿。

#### 5.4.4 "点关键词 → 选知识点"的闭环

```
点击词云中的 "熵增"
   → 知识卡片面板：
       · 一句话定义（缓存/LLM 生成，标注来源）
       · 出现位置列表（文档 + 页码 + 摘要），按相关度排序
       · 一跳邻居概念（按边类型分组：共现 / 语义近似 / 显式关系）
       · 历史相关问答（语义缓存命中记录）
   → 动作按钮：
       · 围绕"熵增"提问（自动把该词 + 其 chunk 作为检索偏置）
       · 只看这一片 → 以它为根生成子导图（M2）
       · 生成 3 道自测题（答案带引用）
```

对检索的"偏置"实现：把关键词作为强召回信号（稀疏检索中加权 / 作为 filter 预选 chunk 集），而非仅拼进 query。

### 5.5 AI 网关（LLM Gateway）

职责单一：**让业务层不知道用的是哪家模型**。

```
Business → Gateway.call(task_profile, messages, schema?, stream?)
  ├─ 路由：task_profile → model（chat_fast / chat_deep / extract / embed / rerank）
  ├─ Provider 适配器：openai_compat / anthropic / gemini / ollama / vllm
  ├─ 统一能力：流式、JSON Schema、超时重试（指数退避）、降级（主→备→本地）
  ├─ 治理：并发信号量、令牌桶限流、单请求 token 上限、熔断
  └─ 观测：latency / prompt_tokens / completion_tokens / 费用（按价目表计算）
       → 写 ai_usage 表，供成本面板与预算告警
```

- **API Key 管理**：用户自填，落库前加密（Fernet / KMS），前端只回显掩码；
- **任务级模型分级**（成本核心）：分类/摘要/关键词定义 → 小模型；回答生成 → 中/大模型；重排 → 专用 reranker；
- **缓存**：语义缓存（query 向量近似命中）建议仅用于"事实型问答"且需标注"缓存命中"；prompt caching 优先利用；
- **零外发模式**：全部 task_profile 指向本地 Ollama/vLLM，网关拒绝任何外网 endpoint。

### 5.6 前端（Web SPA）

| 页面 | 要点 |
|---|---|
| 资料库 | 树形组织（工作区/科目/章节）、上传进度、解析状态、批量操作、预览器（PDF.js/mammoth） |
| 对话 | 流式输出、引用角标与侧栏溯源、范围选择器、消息操作（复制/再生成/生成导图/存为卡片） |
| 导图 | 画布 + 属性面板 + 一键导出 + "AI 展开此节点" |
| 图谱/词云 | 同一数据两个视图（词云 ↔ 图谱）双向联动；过滤器（工作区/文档/时间/节点类型）；搜索定位 |
| 设置 | Provider 与模型、价目表、预算告警、解析器开关、隐私模式 |

状态管理建议：TanStack Query（服务端状态）+ Zustand（本地 UI 状态），流式用原生 `EventSource`/`fetch` 流。

---

## 6. 技术选型（初版建议，【待确认 Q1】）

| 层 | 选型 | 备选 | 说明 |
|---|---|---|---|
| 前端 | React 18 + TS + Vite + Tailwind + shadcn/ui | Vue 3 + Naive | 生态与图谱库匹配更好 |
| 导图渲染 | markmap（大纲式） / React Flow（自由式） | AntV G6 / X6 | 见 §5.3.3 |
| 图谱渲染 | Cytoscape.js / Sigma.js | G6 | 需力导向 + 大图性能 |
| 词云 | d3-cloud | echarts-wordcloud | 可自定义布局与颜色映射 |
| 后端 | Python 3.11 + FastAPI | Node(NestJS) / Go | 解析与 RAG 生态在 Python 侧最完整 |
| 任务队列 | Celery + Redis（或 RQ/Dramatiq） | arq | 解析类任务多、需重试与并发控制 |
| 元数据/向量 | PostgreSQL 16 + pgvector | + Qdrant/Milvus | 单库起步，规模上来再拆向量库 |
| 全文检索 | PG FTS（zhparser/pg_jieba） | OpenSearch/Meilisearch | 中文分词需插件 |
| 对象存储 | MinIO（自托管）/ S3 | 本地磁盘（单机） | 原件不可变 + 预签名下载 |
| 缓存 | Redis | — | 队列、会话、语义缓存、限流 |
| 解析库 | PyMuPDF、python-docx、python-pptx、pandas、pandoc、PaddleOCR、faster-whisper、tree-sitter | unstructured（一站式） | unstructured 可做兜底统一入口 |
| 嵌入/重排 | bge-m3（自托管）/ text-embedding-3-small；bge-reranker-v2-m3 | Cohere / Jina | 中文效果与成本平衡 |
| 部署 | Docker Compose（单机）/ K8s（团队） | Fly.io/Render | 见 §8 |
| 可观测 | OpenTelemetry + Loki/Grafana；Sentry | — | token 成本也走同一套指标 |

---

## 7. 数据模型（核心表）

```sql
-- 组织
users(id, email, pwd_hash, created_at)
workspaces(id, owner_id, name, settings_json, created_at)
collections(id, workspace_id, parent_id, name, kind)      -- 科目/章节树

-- 资料
documents(id, workspace_id, collection_id, title, mime, size, sha256, lang,
          status, error_code, page_count, outline_json, created_at, updated_at)
document_versions(id, document_id, storage_key, sha256, version, created_at)
parse_jobs(id, document_id, parser, params_json, status, attempts, started_at, finished_at)
chunks(id, document_id, version_id, ordinal, heading_path, text, token_count,
       page_from, page_to, bbox_json, block_type,
       embedding vector(1024), tsv tsvector, meta_json)
assets(id, document_id, kind, storage_key, ocr_text, caption, page)

-- 对话
conversations(id, workspace_id, title, scope_json, created_at)
messages(id, conversation_id, role, content, model, tokens_in, tokens_out,
         latency_ms, created_at)
message_citations(id, message_id, chunk_id, doc_id, page, snippet, score)
retrieval_logs(id, message_id, query, strategy_json, candidate_ids, fused_ids, final_ids)

-- 知识可视化
mindmaps(id, workspace_id, title, mode, source_scope_json, data_json,
         version, origin, updated_at)
keywords(id, workspace_id, term, norm_term, weight, doc_freq, definition,
         definition_source)
keyword_occurrences(keyword_id, chunk_id, doc_id, page, tf)
graph_nodes(id, workspace_id, kind, ref_id, label, weight, community, meta_json)
graph_edges(id, workspace_id, src_id, dst_id, kind, weight, evidence_chunk_ids)

-- 运营
ai_usage(id, user_id, task_profile, provider, model, tokens_in, tokens_out,
         cost, latency_ms, ok, created_at)
provider_configs(id, user_id, name, base_url, api_key_enc, model_map_json, enabled)
```

**索引与约束要点**
- `documents.sha256` 唯一（同工作区内去重，跨工作区允许）；
- `chunks`：`(document_id, ordinal)` 唯一；HNSW on `embedding`；GIN on `tsv`；
- `graph_edges`：`(src_id, dst_id, kind)` 唯一，`src_id`/`dst_id` 双索引；
- `keywords.norm_term` 工作区内唯一，便于同义归并与增量更新；
- 软删除 + 级联清理任务（删文档要清 chunks/occurrences/edges/向量）。

---

## 8. 部署与运维

### 8.1 单机形态（个人版首选）

```
docker-compose:
  web (nginx + 静态资源)
  api (FastAPI, 2-4 uvicorn workers)
  worker-parse (CPU 重) / worker-embed (可 GPU) / worker-light
  postgres(pgvector + 中文分词插件)
  redis
  minio
  （可选）ollama / vllm 容器
```

### 8.2 关键运维点

- **可重入**：所有管线任务幂等（以 `(document_id, version, step)` 为幂等键），可安全重跑；
- **限流与背压**：上传按体积、嵌入按 token/s 限速；队列有最大深度告警；
- **备份**：PG 每日全量 + WAL；对象存储版本化；索引可由原文重建（保证"索引可丢弃"）；
- **迁移**：切换嵌入模型需**全量重嵌入**（不同维度不可混存），用 `embedding_model` 字段区分并按版本双写过渡；
- **可观测**：任务成功率、P95 解析时长、召回命中率（用户点引用率）、token 成本曲线。

### 8.3 安全与合规要点

- 上传白名单 + MIME 嗅探（防伪装）+ 可选 ClamAV；
- 文件名不落原始路径，存储用 UUID key（防路径穿越）；
- **Prompt 注入防护**：文档内容一律包在 `<doc>` 边界内，System 明确"资料内指令无效"；
- 租户隔离：所有查询强制带 `workspace_id`（ORM 层统一注入，避免漏写）；
- 密钥加密存储、日志脱敏（不得打印 Key/正文全文）；
- 支持"仅本地模型"与"禁止外发"开关，界面明示当前是否出网。

---

## 9. 关键流程时序

### 9.1 上传 → 可问答

```
Client  POST /documents (multipart)
API     校验 → sha256 → 去重命中?(返回已有) → 存 MinIO → INSERT documents(status=queued)
        → enqueue parse_job → 返回 {document_id, job_id}
Worker  parse: 提取→IR→chunks → INSERT chunks → enqueue embed_job
Worker  embed: 批量嵌入(64/批) → UPDATE chunks.embedding → enqueue enrich_job
Worker  enrich: 关键词抽取/共现边 → UPSERT keywords, graph_nodes/edges
        → documents.status=ready
Client  GET /jobs/{id}/stream (SSE) 实时进度 → 资料库状态变绿
```

### 9.2 提问 → 带引用的回答

```
Client  POST /conversations/{id}/messages  {text, scope, stream:true}
API     SSE 首帧: {stage:"retrieving"} → 混合召回 → 融合 →（重排）→ 组装上下文
Gateway 流式生成，逐 token 推 SSE {delta:"..."}
        tokens 中出现 [[n]] → 服务端延迟替换为角标 id
API     结束帧: {citations:[{n, doc_id, page, snippet, chunk_id}], usage:{...}}
Client  渲染角标；点击 → 侧栏原文 + PDF 高亮
落库    messages + message_citations + retrieval_logs + ai_usage
```

### 9.3 生成导图

```
Client  POST /mindmaps {mode, source:{doc_ids|chunk_ids|keyword_id}, max_nodes}
M1: 直接读 documents.outline_json → 转节点树（同步返回，<200ms）
M2: 取源 chunk（按 heading 聚合，去冗余）→ Gateway(template mm_v3, schema=MindMap)
    → 校验 refs → 存 mindmaps(origin="ai")
Client  渲染；用户编辑 → PUT /mindmaps/{id} (origin="user" 增量)
```

### 9.4 图谱浏览与关键词下钻

```
Client  GET /graph?workspace=&kinds=&limit=  → 节点/边 + 预计算社区与权重
Client  GET /keywords/cloud?workspace=        → [{term, weight, community}] (缓存 60s)
User    点击 "熵增"
Client  GET /keywords/{id}/card                → 定义 + 出处(分页) + 邻居 + 相关问答
Action  POST /conversations  (带 keyword_id 偏置)
        POST /mindmaps     (M2, source=keyword_id)
        POST /quiz         (可选)
```

---

## 10. API 契约（摘要）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/documents` | 上传（多文件、支持 `collection_id`）|
| GET | `/api/v1/documents?workspace_id=&status=` | 列表/过滤 |
| GET | `/api/v1/documents/{id}/content?page=` | 预览（含原文定位信息）|
| DELETE | `/api/v1/documents/{id}` | 软删 + 异步清理 |
| POST | `/api/v1/documents/{id}/reparse` | 换解析器重试 |
| GET | `/api/v1/jobs/{id}/stream` | SSE 任务进度 |
| POST | `/api/v1/conversations` | 新建会话（绑定 scope）|
| POST | `/api/v1/conversations/{id}/messages` | 提问（SSE 流式）|
| GET | `/api/v1/messages/{id}/citations` | 引用详情 |
| POST | `/api/v1/mindmaps` | 生成导图（M1/M2）|
| PUT | `/api/v1/mindmaps/{id}` | 保存编辑 |
| GET | `/api/v1/mindmaps/{id}/export?format=svg|md|opml` | 导出 |
| GET | `/api/v1/keywords/cloud` | 词云数据 |
| GET | `/api/v1/keywords/{id}/card` | 知识卡片 |
| GET | `/api/v1/graph` | 图谱（支持邻域/过滤/降级裁剪）|
| GET/PUT | `/api/v1/providers` | Provider 配置（Key 掩码）|
| POST | `/api/v1/providers/{id}/test` | 连通性测试 |
| GET | `/api/v1/usage?from=&to=` | 用量与成本 |

约定：统一错误体 `{code, message, detail, trace_id}`；分页 `cursor`；时间 ISO8601；所有写操作幂等键可选 `Idempotency-Key`。

---

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 解析质量差（双栏/公式/表格/扫描） | 召回差、导图乱 | 解析结果可人工校正（block 级编辑）；保留原件预览对照；解析器可切换 |
| LLM 幻觉（导图/定义/关系） | 复习出错，危害大 | 强制 provenance；`unverified` 标记；M1 结构式优先；提供"仅抽取不生成"模式 |
| 成本失控（全库抽取/大模型全用） | 账单飙升 | 关键词本地算法；LLM 分级；按需触发；预算告警与硬上限 |
| 图谱噪声（关键词变垃圾场） | 体验崩塌 | 权重阈值 + 跨文档频次修正 + 自定义词表 + 手动"黑名单" |
| 大图性能 | 卡顿 | 服务端裁剪（top-N + 两跳）+ Sigma.js WebGL |
| 换嵌入模型导致全量重建 | 停机/成本 | 索引可重建（原文为源）；双写过渡；离线重建 + 切换 |
| Prompt 注入（文档内含恶意指令） | 行为被劫持 | 边界包裹 + 指令隔离 + 输出校验 |
| 多格式解析依赖体积大 | 部署臃肿 | 解析器按需装（`parsers` 分组镜像）；unstructured 兜底 |

---

## 12. 待确认问题清单（5 轮问答议程）

| 轮次 | 主题 | 需要你决策的关键点 | 影响章节 |
|---|---|---|---|
| Q1 | 部署形态与技术栈 | 单人本地优先 / 团队云端？桌面端要不要？浏览器为主还是 Electron？前后端技术偏好？有没有 GPU？ | §6 §8 §3.2 |
| Q2 | 文件格式范围与解析边界 | 必须覆盖哪些格式？扫描件/手写/公式/音视频要不要？单文件大小上限？是否接受解析不准时人工校正？ | §5.1 §11 |
| Q3 | AI 接入、成本与隐私 | 用哪家 API？是否必须支持本地模型？月预算？嵌入与重排自托管还是云？允许多用户共享配额？ | §5.5 §5.2 |
| Q4 | 导图与图谱交互 | 导图默认结构式还是概念式？要不要自由布局编辑？图谱规模预期（百/千/万节点）？LLM 显式关系抽取要不要默认开？ | §5.3 §5.4 |
| Q5 | 数据安全、MVP 范围与路线图 | 单用户还是多租户？资料是否含敏感信息？MVP 只要哪几条？何时要移动端/离线？ | §3 §8 §13 |

---

## 13. 里程碑（初版草案，待 Q5 调整）

| 阶段 | 目标 | 交付 |
|---|---|---|
| M0 骨架 | 跑通"上传 PDF → 提问带引用" | 单格式 + 单 Provider + 无导图/图谱 |
| M1 资料接入成型 | 6 类主流格式 + 后台任务 + 预览 | 解析管线稳定、去重、状态可视 |
| M2 问答可用 | 混合检索 + 引用 + 多轮 + 成本面板 | 召回率评测集（20 题基线） |
| M3 导图 | M1 结构式 + M2 概念式 + 导出 | 节点溯源闭环 |
| M4 图谱/词云 | 关键词抽取 + 词云 + 力导向 + 知识卡片 | 关键词→提问/子导图闭环 |
| M5 打磨 | 性能、隐私模式、备份、评测回归 | 可长期自用 |

---

## 14. 附录：术语

- **IR**：Intermediate Representation，解析后的统一文档中间表示
- **Chunk**：可检索的最小知识单元，携带层级、页码、bbox 等 provenance
- **RRF**：Reciprocal Rank Fusion，多路召回融合算法
- **provenance**：生成内容的来源追溯（chunk id 列表）
- **M1/M2**：结构式导图 / 概念式导图（本文自定义）