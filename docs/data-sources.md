# 数据来源与处理

## 1. 官方数据源

| 数据 | 渠道 | 接入方式 |
|---|---|---|
| 金融法规 | 国家金融监督管理总局、证监会、人民银行、国家法律法规数据库 | 官方页面/授权接口，保存标题、文号、发布机关、URL |
| 公募基金 | 巨潮资讯、证监会基金电子披露、基金管理人官网 | 招募说明书、产品资料概要、定期报告 |
| 保险产品 | 中国保险行业协会产品信息库、保险公司官网 | 备案条款、费率表、产品说明书 |
| 银行理财 | 中国理财网、理财公司官网 | 产品说明书、风险揭示书、净值公告 |
| 研报 | Wind、Choice、iFinD 或券商授权渠道 | 企业授权接口，禁止未授权批量抓取 |
| FAQ | 机构客服知识库、持牌投教内容 | 内部导入，入库前去重和脱敏 |

## 2. 当前仓库数据

- `data/kb/regulations/`：39 部官方法规 PDF，附带 `.meta.json`；
- `data/raw/fund/`：基金说明书示例；
- `data/raw/insurance/`：保险条款示例；
- `data/raw/research/`：行业研报示例；
- `data/raw/faq/faq.jsonl`：FAQ 数据；
- `data/eval/qa.jsonl`：检索与回答评测集。

示例数据仅用于工程验证，生产环境必须替换为机构授权数据。

## 3. 文档加载

| 格式 | 处理方式 |
|---|---|
| PDF | pypdf；复杂版式可接 MinerU |
| DOCX | python-docx，保留段落、表格和图片关系 |
| HTML | 标准库 HTMLParser，提取标题、正文、列表、表格 |
| Markdown | 保留标题层级、条款和图片标记 |
| CSV/XLSX | 转为表格文本，按行或模块切分 |

图片保存到 `data/images/{doc_id}/`，通过 `/v1/images/{id}` 返回，并在引用和回答中显示。

## 4. 分块策略

配置位于 `configs/default.yaml` 的 `ingestion.profiles`：

| 数据类型 | 策略 | 默认参数 |
|---|---|---|
| 研报 | 章节语义分块，保留核心观点、投资建议、风险提示 | `max_chars=512, overlap=64` |
| 基金说明书 | 按投资策略、风险收益、费率、历史业绩成块 | `max_chars=800, overlap=80` |
| 保险条款 | 条款号/章节优先，表格保护 | `max_chars=520, overlap=100` |
| 通用文档 | 标题边界优先，句边界滑动窗口 | `max_chars=520, overlap=100` |

每个 chunk 保留文档、章节、条款号、页码、风险等级、发布日期和图片关联。

## 5. 向量化与存储

- 生产：BGE-M3，1024 维；
- 本地/CI：确定性哈希向量；
- 金融术语：`configs/domain_dict.txt` 自定义词典；
- 向量库：本地文件向量库或 Milvus；
- Collection：`fund`、`insurance`、`bond`、`bank_wealth`、`equity`、`macro`。

## 6. FAQ 检索

```text
Redis 高频 FAQ 缓存
        ↓ miss
MySQL/SQLite FAQ 库
        ↓ miss
BM25 模糊匹配
        ↓ miss
RAG 检索与生成
```

Redis 不可用时降级到 FAQ 库 + BM25，不影响主链路。

## 7. 增量更新与验收

- 研报、净值和新闻按授权接口增量同步；
- 每条文档保留 `publish_date` 和 `effective_date`；
- 研报查询可按最近 90 天过滤；
- 验收指标按实际接入规模测量：索引耗时、Recall@5、MRR、引用可回溯率、P95 延迟、错误率和合规拦截率。

不要用示例数据或本地压测结果替代生产验收。
