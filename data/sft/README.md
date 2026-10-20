# SFT 数据目录

`seed.jsonl` 是**演示用合成种子数据**，只用于验证数据清洗、合规过滤、去重和
instruction/ChatML 导出流程，不是 6 万条分析师标注数据，也不能用于真实产品推荐。

生产数据请放入 `raw/`（不提交到仓库），并满足：

- 来源可授权、可追溯；
- 每条有 `source_id`（同一研报/产品文档的多个问答会分到同一 split）；
- 经金融分析师或持牌顾问复核；
- 不含真实客户 PII；
- 投资建议包含风险提示，产品推荐通过适当性检查。

```powershell
python scripts/prepare_sft.py `
  --input data/sft/raw/all.jsonl `
  --out data/sft/processed `
  --target 60000 `
  --min-quality 65
```
