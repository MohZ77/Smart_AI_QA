# 用户画像与检索策略

## 1. 目标

企业金融顾问场景里，画像不是简单的 Prompt 变量，而是三类约束：

```text
画像
  ├─ 硬约束：风险等级、适当性、产品范围
  ├─ 软改写：经验、期限、资产档位 → query 扩展
  └─ 缓存/审计：画像桶、版本、租户、快照
```

原则：**风险等级由确定性规则控制，经验和期限可以影响检索与解释，但不能覆盖适当性红线。**

## 2. 当前实现

画像字段：

```text
risk_level          C1-C5
investment_experience
horizon
investable_assets
profile_version
tenant_id
```

### 2.1 画像分桶

为了避免把精确资产金额和自由文本放进缓存键，系统把画像映射为桶：

| 维度 | 分档 |
|---|---|
| 风险 | C1 / C2 / C3 / C4 / C5 |
| 经验 | novice / intermediate / expert |
| 期限 | short / medium / long |
| 资产 | unknown / under_50w / 50_300w / 300_600w / over_600w |

缓存指纹格式：

```text
{tenant_id}|{risk}|{experience}|{horizon}|{asset_band}|v{profile_version}
```

同一个资产档位内，300 万和 450 万共享缓存；画像更新后版本号递增，旧缓存自动失效。

### 2.2 策略改写

`QueryPlanner` 保留原始问题，并追加一条受控的画像扩展查询：

```text
原始问题：推荐几只基金

C2 / 1年以内 / 短期 / 50万以下：
  追加：低风险 货币基金 债券基金 R1 R2 适当性
        短期 流动性 货币基金 短债
        通俗解释 费用 风险提示
        起投金额 低门槛

C5 / 5年以上 / 长期 / 500万以上：
  追加：权益类 混合基金 R4 R5 风险收益特征
        长期 3-5年 资产配置
        最大回撤 夏普比率 基金经理 持仓
        资产配置 组合分散
```

SubQuery 场景不额外追加画像查询，避免稀释已有子查询的召回；画像主要通过风险过滤和 Prompt 生效。

### 2.3 硬过滤

推荐类问题会将画像风险等级转换为 metadata 过滤：

```text
C1 → R1
C2 → R1-R2
C3 → R1-R3
C4 → R1-R4
C5 → R1-R5
```

高风险产品不会因为 Prompt 或 query 改写而进入低风险用户的候选集。

### 2.4 审计快照

审计记录保存画像快照：

```json
{
  "tenant_id": "default",
  "risk_level": "C3",
  "experience_band": "intermediate",
  "horizon_band": "medium",
  "asset_band": "50_300w",
  "profile_version": 2
}
```

不保存精确资产金额，避免把敏感财务数据写入审计日志；需要时可结合用户授权记录还原画像版本。

## 3. 高并发设计

- 画像读取使用进程内 TTL-LRU 缓存（默认 30 秒），减少每轮请求直查数据库。
- 画像更新后递增 `profile_version` 并清理本地缓存。
- 生产多实例部署建议将画像缓存迁移到 Redis，并通过更新事件广播失效。
- 缓存键包含 `tenant_id`、知识库版本、Prompt 版本和画像桶，避免跨租户/跨版本串答案。
- 热点问题可增加 single-flight，防止缓存击穿。

## 4. 后续演进

1. **顾问/客户双身份**：企业工作台里，`operator_id` 是顾问，`customer_id` 才是画像主体；需要客户授权和租户权限。
2. **画像过期与重评**：风险测评设置有效期，资产和期限变化时触发重新评估。
3. **多租户隔离**：缓存、数据库和 Milvus 分区都带 `tenant_id`。
4. **画像专项评测**：同一问题在 C2/C5、短期/长期、新手/老手画像下必须产生不同过滤和解释。
5. **Redis 画像缓存**：多实例共享，更新事件驱动失效。

## 5. 测试

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m pytest -q tests/test_profile_policy.py tests/test_retrieval_e2e.py tests/test_auth.py tests/test_auth_and_audit.py
```

覆盖画像分桶、缓存指纹、query 扩写、风险过滤、画像版本递增和审计快照。
