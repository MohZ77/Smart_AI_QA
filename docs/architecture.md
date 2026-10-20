# 架构与数据流

## 1. 总体设计

系统由**离线索引**和**在线问答**两条链路组成，共享同一套文档元数据与 Collection 路由。

### 离线索引

```text
PDF / DOCX / MD / TXT
  → MinerU（可选，扫描件/复杂版式）
  → 清洗（页眉页脚、页码、PII、重复空行）
  → 结构感知分块（标题/条款/表格/句边界）
  → Embedding（BGE-M3 1024d）
  → Milvus（equity/fund/insurance/bond/bank_wealth/macro）
  → MySQL（文档、章节、页码、risk_level、industry、publish_date）
  → BM25（关键词召回 + FAQ 近似匹配）
```

### 在线问答

```text
问候检测
  → 输入合规预检（收益承诺/内幕消息/返佣等）
  → 意图识别 + 实体抽取（条款号、产品、行业、风险等级）
  → 三级缓存（进程内 → Redis → MySQL FAQ/BM25）
  → 策略路由（Direct / HyDE / SubQuery / Backtrack）
  → 多 Collection 并行混合检索（向量 + BM25）
  → RRF 融合 + Rerank
  → 最近 5 轮对话拼接
  → LLM 生成（DeepSeek / Qwen / 离线抽取）
  → 输出合规审查（收益承诺/适当性/引用/免责声明）
  → WebSocket / SSE 流式返回
  → 审计落库
```

## 2. 模块职责

| 模块 | 职责 | 关键文件 |
|---|---|---|
| 解析 | MinerU/pypdf/docx/md，保留页码与 frontmatter | `ingestion/loaders.py` |
| 清洗 | 页眉页脚、页码、PII、重复行 | `ingestion/chunking.py`、`utils.py` |
| 分块 | 标题/条款边界、句边界滑窗、表格保护、parent-child | `ingestion/chunking.py` |
| 索引 | 批量 Embedding、增量写入、BM25 重建 | `ingestion/pipeline.py` |
| 向量库 | 本地 numpy / Milvus 六库路由 | `indexing/collection_store.py`、`vector_store.py` |
| 关键词 | BM25-Okapi、条款号加权 | `indexing/bm25.py` |
| FAQ | 精确 + BM25 近似 + Jaccard 兜底 | `indexing/faq.py` |
| 查询理解 | 规则意图 + 可选 LLM 分类、实体抽取 | `query/understanding.py` |
| 路由/改写 | Direct/HyDE/SubQuery/Backtrack、同义词扩展 | `query/understanding.py` |
| 检索 | 并行召回、RRF、Rerank、置信度 | `retrieval/hybrid.py`、`rerank.py` |
| 生成 | Prompt、OpenAI 兼容 LLM、流式、重试 | `generation/llm.py`、`prompts.py` |
| 编排 | 缓存、FAQ、路由、检索、合规、审计 | `generation/answerer.py` |
| 合规 | 输入预检、规则引擎、LLM 复核、免责声明 | `compliance/rules.py` |
| 存储 | SQLite/MySQL 双后端 | `storage/db.py`、`memory.py`、`audit.py` |
| 服务 | FastAPI、SSE、WebSocket、限流、指标 | `service/app.py` |
| 增量同步 | Wind/基金净值/新闻 JSONL 适配 | `sync/pipeline.py` |

## 3. 数据模型

`Chunk` 的核心字段：

```text
chunk_id, doc_id, text, title, section, article, page, page_end
parent_text, doc_type, asset_class, industry, publish_date, risk_level
product_code, effective_date, metadata{source_path, content_hash, ...}
```

- `parent_text`：所在章节的完整上下文，用于生成阶段扩展；
- `article`：条款号（如“第三条”），用于精确匹配与引用；
- `asset_class`：路由到六库之一；
- `industry`：研报行业分区；
- `publish_date`：时效过滤；
- `risk_level`：适当性过滤。

## 4. 分块策略

1. **标题优先**：Markdown 标题、`第X章/第X条`、`一、`、`（一）`、数字编号作为 section 边界；
2. **句边界滑窗**：section 超过 `max_chars` 时按中文句末标点切分，保留 `overlap_chars` 尾部句子；
3. **表格保护**：表格行不按字符切断，按 `table_max_chars` 分组；
4. **父子块**：child 用于召回，parent 用于生成，兼顾精度与上下文；
5. **页码映射**：每个 chunk 记录起止页码，引用可回跳原文。

### 图片关联

- PDF：解析每页图片，chunk 的 `page_start/page_end` 覆盖图片页码时建立关联；
- Markdown：图片替换为 `[[IMAGE:id]]` 标记，标记所在 section 关联图片；
- DOCX：读取 inline shape 的图片关系并在文档末尾追加标记，复杂浮动版式建议走 MinerU；
- 图片落盘到 `data/images/{doc_id}/`，由 `ImageStore` 维护索引；
- `Citation.images` 返回原始图片 URL，`Answerer` 在回答末尾追加 `![alt](/v1/images/{id})`；
- 前端 `/v1/images/{id}` 渲染缩略图和原图，不接受任意文件路径。

金融条款中“第三条 等待期”与“第四条 免责”混在一个 chunk 里会污染检索；标题边界优先可以降低这种噪声。

## 5. 检索与排序

### 5.1 并行召回

- 向量：每个 Collection `vector_top_k=30`；
- BM25：全库 `bm25_top_k=30`，按 `asset_class`/`risk_level`/`industry`/`publish_date` 过滤；
- 子查询：按 `SubQuery` 拆分后并行执行，分别路由到 `macro`、`fund` 等 Collection；
- 线程池：IO/CPU 混合场景用 `ThreadPoolExecutor`，避免串行等待。

### 5.2 RRF

```text
score(d) = Σ 1 / (k + rank_i(d))
```

只依赖排名，避免向量余弦与 BM25 分数量纲不一致。默认 `k=60`，融合后取 `fusion_top_k=20`。

### 5.3 Rerank

- `heuristic`：0.50 × RRF 归一化 + 0.30 × 查询词覆盖 + 0.14 × 条款号命中 + 0.06 × 标题命中；
- `cross-encoder`：`BAAI/bge-reranker-base`，不可用时自动回退启发式；
- 同一章节多个 child 只保留最靠前的一个，避免上下文被重复段落占满。

### 5.4 置信度

```text
confidence = 0.65 × top_score + 0.35 × query_token_coverage(top3)
```

低于 `retrieval.min_confidence`（默认 **0.80**）直接拒答，不调用生成模型，也不把低相关 chunk 作为引用展示。该阈值应在真实评测集上校准，而不是拍脑袋。

## 6. 合规设计

### 输入层

- 拦截“保证赚钱/稳赚/内幕消息/洗钱/返佣”等明确违规请求；
- 对“销售可以承诺收益吗”这类**合规咨询**放行，避免误杀；
- 问候语单独处理，不进入 RAG。

### 输出层

- 规则引擎：收益/赔付承诺、保本表述、绝对化宣传、销售误导、违规利益；
- 适当性：产品风险等级 Rk 与用户 Ck 比较，超出 `max_risk_gap` 拦截；
- 引用：要求答案出现 `[n]`，且引用来自本次检索；
- 免责声明：自动追加“不构成投资建议/销售承诺，以合同及审核结果为准”；
- 可选 LLM 复核：第二层判断，失败时回退规则结果；
- 审计：请求、回答、引用、合规结论、模型、延迟全量落库。

## 7. 缓存与一致性

| 层级 | 实现 | 失效 |
|---|---|---|
| L1 | 进程内 TTL-LRU | TTL + LRU |
| L2 | Redis | TTL |
| L3 | MySQL FAQ + BM25 | 数据更新时重建 |

答案缓存 key 包含：

```text
normalized_question + risk_level + model + kb_version
```

知识库重建后 `kb_version` 变化，旧答案自然失效。多副本部署时 Redis 共享 L2，L1 允许短暂不一致。

## 8. 扩展与容量

- API 无状态，可直接水平扩容；
- Milvus 六库可按资产类别独立扩容；
- LLM 客户端 `concurrency` 控制上游并发，避免打爆推理服务；
- Redis 承担热点问答缓存，降低 LLM 与向量库压力；
- MySQL 生产建议连接池 + 主从/只读副本；
- 生产容量需按实际在线人数、峰值 QPS 和峰均比，用 `scripts/load_test.py` 在多副本环境实测，并按突发倍数留余量；不能由单机代码推断。

## 9. 失败模式

| 故障 | 行为 |
|---|---|
| Redis 不可用 | 自动降级 L1 + FAQ |
| Milvus 不可用 | 启动失败（生产）或切换 local（开发） |
| LLM 超时/429 | 指数退避重试，最终返回错误/拒答 |
| 检索置信度低 | 在线 LLM 做受控通用分析并标注知识库未命中；离线模式先澄清一次，仍无依据则明确结束，避免循环追问 |
| 监管文件标题被误判 | 标题含“基金/保险”时优先按法规引用识别，路由到监管库并过滤 `doc_type=regulation` |
| 知识库没有该产品 | 返回目录核对结果，明确“未收录”，不生成条款 |
| 图片文件缺失 | 保留文字答案，引用中不返回失效图片 |
| 合规不通过 | 严格重写一次；仍不通过则安全兜底 |
| FAQ/BM25 无命中 | 进入 RAG 链路 |
| 索引任务失败 | 任务状态 `failed`，保留旧索引 |
