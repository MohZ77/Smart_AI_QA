# 离线知识库目录

这里只定义**目标结构和数据规范**，仓库没有打包图片中的 5 万法规、3 万产品文档、10 万研报和 6 万 FAQ。

把已授权的数据放入对应目录：

```text
data/kb/
  regulations/       金融法规 PDF/HTML/MD/TXT
  products/          基金/保险/债券/银行理财说明书
  research/          券商研报 PDF/MD
  faq/               金融 FAQ JSONL/CSV
```

FAQ JSONL 至少包含：

```json
{"question": "基金怎么买？", "answer": "...", "category": "product_consultation", "source": "授权来源"}
```

然后统计目标缺口：

```powershell
python scripts/build_kb.py
```

统计并重建索引：

```powershell
python scripts/build_kb.py --ingest --rebuild
```

`data/kb/manifest.json` 会记录每类数据的文件数、有效条数、重复数和与目标的缺口。不要用随机生成文档补足目标数量。
