# 基金、保险、理财产品的数据获取与入库

## 1. 渠道优先级

按合规性和可追溯性从高到低：

| 优先级 | 渠道 | 用途 | 说明 |
|---:|---|---|---|
| P0 | 中国理财网 | 银行理财说明书、合同、风险揭示、净值公告 | 银行理财官方登记披露，首选基准 |
| P0 | 巨潮资讯网 / 证监会基金电子披露 | 公募基金招募说明书、基金合同、定期报告 | 法定披露平台，优先于代销页面 |
| P0 | 中国保险行业协会产品信息库 | 保险备案条款、费率表、产品说明 | 保险产品官方备案版本 |
| P1 | 发行机构官网 | 最新产品公告、补充材料、盖章版本 | 用于校验和获取最新版本 |
| P2 | Wind / iFinD / Choice / 普益标准 | 批量元数据、文档、每日增量 | 企业授权数据，适合生产环境 |
| P3 | 银行/互联网金融代销平台 | 产品详情、交易规则、用户问答 | 只作补充，不能替代官方披露 |
| P4 | 官方公开页面定向低速采集 | 无法通过接口获取的公开披露 | 必须遵守 robots、频率、授权和版权要求 |

**建议**：生产知识库的“基准事实”只使用 P0/P1；代销平台内容用于 FAQ 和用户体验补充，不单独作为条款依据。

## 2. 各类产品建议

### 基金

- 首选：巨潮资讯网、证监会基金电子披露、基金公司官网；
- 文档：招募说明书、基金合同、产品资料概要、季报/年报、临时公告；
- 关键 metadata：基金代码、基金类型、风险等级、基金经理、成立日期、费率、公告日期。

### 保险

- 首选：中国保险行业协会产品信息库、保险公司官网；
- 文档：备案条款、费率表、产品说明书、理赔指南；
- 关键 metadata：产品代码、险种、保险公司、备案日期、风险等级/适当性、等待期。

### 银行理财

- 首选：中国理财网、理财公司官网；
- 文档：产品说明书、风险揭示书、产品合同、净值/定期报告；
- 关键 metadata：产品登记编码、发行机构、风险等级、期限、开放规则。

### 债券/固收产品

- 来源：发行机构、交易所/银行间市场披露、授权数据服务商；
- 重点：久期、信用等级、票息、风险等级、发行日期。

## 3. 批量导入格式

不要直接把不同来源的 PDF 混在一个目录里。先用产品清单登记来源和 metadata：

```csv
id,title,asset_class,doc_type,product_code,risk_level,publish_date,official_url,local_path,source,license
fund-000001,消费主题基金测试说明书,fund,fund,F000001,R3,2026-09-20,,data/raw/fund/消费主题基金示例说明书.md,test-fixture,internal-test-only
```

字段说明：

| 字段 | 说明 |
|---|---|
| `asset_class` | `fund` / `insurance` / `bond` / `bank_wealth` |
| `doc_type` | 产品文档类型，通常与 asset_class 相同 |
| `product_code` | 基金代码、产品登记编码或保险产品代码 |
| `risk_level` | R1-R5，必须保留原始评级 |
| `publish_date` | 公告/版本日期 |
| `official_url` | 官方披露页面或文件 URL |
| `local_path` | 已下载文件路径；与 official_url 二选一 |
| `source` / `license` | 来源和授权说明 |

## 4. 导入命令

先检查清单：

```powershell
python scripts\import_product_docs.py `
  --manifest data/kb/products/MANIFEST.csv `
  --check-only
```

确认后导入：

```powershell
python scripts\import_product_docs.py `
  --manifest data/kb/products/MANIFEST.csv
```

只有在确认来源允许下载时，才使用：

```powershell
python scripts\import_product_docs.py `
  --manifest data/kb/products/MANIFEST.csv `
  --download --delay 0.5
```

导入后会生成：

```text
data/kb/products/fund/...pdf
data/kb/products/fund/...pdf.meta.json
```

sidecar 保留产品代码、风险等级、发布日期、官方 URL 和授权信息，供引用与审计。

## 5. 入库与更新

```powershell
python scripts\build_kb.py
python scripts\build_kb.py --ingest --rebuild
```

建议按产品代码和内容 hash 做增量更新：

- 产品代码相同、内容 hash 变化 → 替换旧版本；
- 旧公告/旧版说明书保留 `publish_date`，检索时优先采用最新版本；
- 产品停售/下架时保留历史条款，但标记 `status=inactive`，避免和当前产品混淆。

## 6. 合规边界

- 不绕过登录、付费墙或反爬机制；
- 不批量抓取代销平台的用户评论、持仓或个人信息；
- 研报全文使用必须有授权；
- 保险条款和基金合同必须保留备案/公告版本和来源链接；
- 产品风险等级以发行机构或官方披露为准，不能用模型推测。
