# 离线知识库构建

## 1. 当前仓库数据

| 数据类型 | 当前内容 | 来源 |
|---|---|---|
| 金融法规 | 39 部 PDF | 国家金融监督管理总局官方法规库 |
| 基金文档 | 示例说明书 | `data/raw/fund/` |
| 保险文档 | 示例条款与说明书 | `data/raw/insurance/` |
| 研报 | 示例行业研报 | `data/raw/research/` |
| FAQ | 23 条 | `data/raw/faq/faq.jsonl` |

这些数据用于验证解析、分块、向量化和检索链路，不代表生产知识库规模。

## 2. 目录结构

```text
data/kb/
  sources.yaml       数据源与接入说明
  regulations/       金融法规
  products/          基金/保险/债券/银行理财产品文档
  research/          研究报告
  faq/               金融 FAQ JSONL/CSV
  manifest.json      文件数、有效记录、重复数、错误数
```

## 3. 导入与构建

把已授权的数据放入对应目录，然后运行：

```powershell
python scripts\build_kb.py
```

统计后重建统一索引：

```powershell
python scripts\build_kb.py --ingest --rebuild
```

`manifest.json` 会按实际文件计算数量，不预设目标数。

## 4. 数据质量门禁

- **授权**：来源、许可证、可使用范围必须可追溯；
- **去重**：文件 SHA-256 去重；FAQ 做问题级近似去重；
- **PII**：客户姓名、手机号、身份证、银行卡在入库前脱敏；
- **元数据**：`doc_type`、`asset_class`、`publish_date`、`source`、`license`；
- **格式**：PDF/HTML/Markdown/TXT/CSV/XLSX；扫描 PDF 先做 OCR；
- **时效**：研报和新闻带发布日期，查询时可按最近 90 天过滤；
- **合规**：不导入含收益承诺、内幕信息、未授权销售话术的文本。

## 5. 增量更新

生产环境建议按以下流程更新：

1. 从授权数据源拉取新文件；
2. 用文件 hash 判断是否新增或变更；
3. 重新解析变更文件，保留旧索引直到新版本校验通过；
4. 切换索引版本并记录 `kb_version`；
5. 对新增法规和产品条款抽样人工核验。

## 6. 生产接入边界

法规、研报、基金说明书和保险条款的版权、转载和衍生使用范围必须由接入方确认。仓库不使用生成文本补齐法规或产品数据，也不把示例文档当作真实业务数据。
