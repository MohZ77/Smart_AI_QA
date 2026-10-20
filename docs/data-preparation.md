# SFT 数据准备

以 **60,000 条**作为示例目标，为金融领域模型/LoRA 准备经分析师复核的指令数据，覆盖股票、基金、保险、债券、银行理财、宏观经济六类资产。仓库提供可复现的清洗、合规、去重、切分和导出流水线；`data/sft/seed.jsonl` 只有 8 条合成样例，不代表已完成 6 万条数据。

## 1. 数据配比建议

| 资产类别 | 目标条数 | 典型内容 |
|---|---:|---|
| 股票/权益 | 10,000 | 行业景气、估值、公司研究、风险提示 |
| 基金 | 10,000 | 产品对比、费率、回撤、基金经理、适当性 |
| 保险 | 10,000 | 条款解读、等待期、免责、理赔、核保 |
| 债券/固收 | 10,000 | 久期、信用、利率、回撤、组合配置 |
| 银行理财 | 10,000 | 净值型产品、期限、风险等级、流动性 |
| 宏观经济 | 10,000 | 利率、政策、行业趋势、资产配置 |

实际配比应按业务流量调整，但每一类都要有足够的“拒答/资料不足/不合规请求”负样本。

## 2. 数据来源与授权

允许的来源：

- 券商研究所公开研报摘要（确认转载/衍生使用授权）；
- 持牌理财师创作的合规投教内容；
- 金融监管政策 FAQ、公开法规；
- 经脱敏和授权的历史顾问工单；
- 人工编写的边界案例和拒答样本。

不应直接使用：

- 未授权的付费研报全文；
- 含真实客户姓名、手机号、身份证、银行卡、持仓的记录；
- 带销售话术、收益承诺、内幕信息的聊天记录；
- 未经复核的纯 LLM 生成答案。

每条数据必须保留 `source`、`source_id`、`license`、`publish_date`，便于追溯和删除。

## 3. 字段规范

输入既支持业务方给出的 `instruction/input/output`，也兼容 `question/answer`。

```json
{
  "id": "fund-000001",
  "instruction": "消费板块基金现在值得配置吗？",
  "input": "用户风险偏好 C3，投资期限 3-5 年",
  "output": "……不构成投资建议；基金过往业绩不代表未来表现……",
  "asset_class": "fund",
  "intent": "investment_strategy",
  "risk_level": "C3",
  "source": "licensed-research",
  "source_id": "research-doc-001",
  "publish_date": "2026-09-20",
  "license": "authorized",
  "quality_score": 86.0
}
```

`source_id` 很重要：同一份研报/产品说明书派生的多个问答必须分到同一个 split，避免训练集和验证集泄漏。

## 4. 流水线

```text
原始 JSONL
  → 字段校验 / 文本规范化 / PII 脱敏
  → 资产类别、意图、风险等级归一化
  → 合规过滤（收益承诺 / 保本 / 绝对化 / 适当性）
  → 风险提示补全
  → 质量打分
  → 精确去重 + SimHash 近似去重
  → 按 source_id 分组、按资产类别分层切分
  → instruction JSONL + ChatML JSONL + manifest
```

运行：

```powershell
python scripts/prepare_sft.py `
  --input data/sft/raw/all.jsonl `
  --out data/sft/processed `
  --target 60000 `
  --min-quality 65
```

输出：

```text
data/sft/processed/
  train.jsonl
  val.jsonl
  test.jsonl
  train.chatml.jsonl
  val.chatml.jsonl
  test.chatml.jsonl
  manifest.json
```

## 5. 质量门禁

| 门禁 | 规则 |
|---|---|
| 合规 | 收益承诺、保本、绝对化赔付、内幕/返佣等直接拒绝 |
| 适当性 | 产品 R 等级高于用户 C 等级且构成推荐时拒绝 |
| 风险提示 | 投资类样本缺少“不构成投资建议/投资有风险/不代表未来”时补全或警告 |
| 保险声明 | 缺少“以合同/条款/核保为准”时补全或警告 |
| PII | 手机号、身份证、银行卡、邮箱在入库前脱敏 |
| 质量分 | 结构、长度、资产类别匹配、风险提示、数字/条款依据等启发式评分 |
| 去重 | 精确哈希 + 64-bit SimHash，Hamming 距离 ≤3 视为近似重复 |
| 切分 | 按 `source_id` 分组，防止同一文档跨 split |

启发式质量分只能做预筛，不能替代金融分析师的人工标注。

## 6. 如何从种子数据扩展到目标规模

1. **先建文档池**：按六类资产收集授权文档，完成解析、清洗和 metadata；
2. **构造问题**：从条款/研报中抽取事实点，生成单跳、对比、多跳、时效、拒答问题；
3. **模型生成候选答案**：用强模型基于原文生成，必须带引用和风险提示；
4. **分析师复核**：核对数字、条件、免责范围、产品风险等级和适当性；
5. **负样本**：保留“资料不足”“不推荐”“风险不匹配”“违规请求拒绝”样本；
6. **合规复核**：100% 规则过滤 + 抽样人工复核；
7. **去重与切分**：按 source_id 分组，训练/验证/测试 90/5/5；
8. **版本化**：记录数据版本、Prompt 版本、模型版本和标注批次。

不要用“让模型生成 6 万条，然后直接当分析师标注”这种方式凑数。那会把模型幻觉固化进训练集，也无法通过数据质量审计。

## 7. 当前样例结果

`scripts/prepare_sft.py` 对 8 条 seed 数据的结果：

```text
total=8
kept=8
low_quality=0
exact_duplicates=0
near_duplicates=0
train=6  val=1  test=1
target_gap=59992
```

`target_gap` 会写入 `manifest.json`，提醒当前数据量未达到 60,000 条。

## 8. 与 LoRA 训练衔接

训练使用 `*.chatml.jsonl`，格式为 system/user/assistant 三段。详见 [model-serving.md](model-serving.md)。

训练前建议：

- 先做 1,000 条小规模 SFT，验证格式和拒答行为；
- 用 `train/val` 做 loss 和人工抽检；
- 在 `test` 上比较“基座 + RAG”“LoRA + RAG”；
- 重点看事实性、适当性、引用覆盖率，而不是只看 loss。
