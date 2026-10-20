# 测试规范与用例

## 1. 运行

```powershell
cd <项目根目录>
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

或直接运行封装脚本：

```powershell
.\scripts\run_tests.ps1
```

每次修改以下任一部分后必须运行：

- 账号、Token、权限和画像；
- 解析/分块/索引；
- 检索、路由、Prompt、LLM 客户端；
- 合规规则；
- 文件/图片上传；
- API、缓存、审计；
- 数据导入脚本。

## 2. 当前覆盖

| 测试文件 | 覆盖点 |
|---|---|
| `tests/test_api.py` | 健康检查、普通问答、WebSocket、画像问卷 |
| `tests/test_auth.py` | 注册、登录、重复用户名、弱密码、Token 过期、身份伪造、会话隔离、会话清理幂等、流式/WS 鉴权 |
| `tests/test_profile_policy.py` | 画像分桶、缓存指纹、query 扩写、资产分档、审计快照 |
| `tests/test_upload_and_frontend.py` | 管理端上传入库、登录后文件审查、登录页契约、登录限流 |
| `tests/test_offline_ingestion.py` | Markdown 文件 → 清洗/分块 → Embedding → 向量库 |
| `tests/test_offline_files_e2e.py` | `data/` 真实 PDF/Markdown → 入库 → 检索 → 问答 → 引用；法规标题意图路由；未知产品不编造；低置信度只澄清一次；违规请求拦截 |
| `tests/test_retrieval_e2e.py` | 检索、引用、组合问题、适当性过滤、知识域过滤、低置信度意图确认 |
| `tests/test_document_review.py` | 上传合同 → 解析/条款检索 → 风险审查 |
| `tests/test_images.py` | Markdown 图片提取、引用图片、图片接口 |
| `tests/test_vision.py` | 图片识别失败不编造、识别文本进入问答、在线兜底 |
| `tests/test_auth_and_audit.py` | API Key 鉴权、异步审计落库 |
| `tests/test_bm25_rrf.py` | BM25、RRF、章节去重 |
| `tests/test_cache.py` | TTL/LRU 缓存 |
| `tests/test_compliance.py` | 收益承诺、适当性、输入预检 |
| `tests/test_chunking.py` | 条款边界、表格保护 |
| `tests/test_dataprep.py` | SFT 清洗、合规过滤、去重、切分 |
| `tests/test_loaders_formats.py` | HTML/CSV/XLSX 解析 |
| `tests/test_logging.py` | 控制台/文件双通道、日志轮转和写入失败降级 |

## 3. 离线文件测试数据

仓库里的 `data/raw/` 和 `data/kb/` 可作为测试语料：

- `data/raw/insurance/平安福示例条款.md`
- `data/raw/fund/基金投资指南示例.md`
- `data/kb/regulations/*.pdf`

测试会把临时文件放在 `.test-tmp/`，不会修改正式知识库文件。

## 4. 登录/注册说明

项目已实现本地账号体系：

```text
POST /v1/auth/register
POST /v1/auth/login
GET  /v1/auth/me
PATCH /v1/auth/profile
Authorization: Bearer <token>
```

密码使用 PBKDF2-SHA256 加盐哈希；Token 使用 HMAC-SHA256 签名并带过期时间。登录后服务端以账号画像为准，客户端不能伪造 `user_id` 或风险等级。匿名体验仍可用，设置 `security.require_auth_for_chat=true` 可强制登录。

完整说明见 [auth.md](auth.md)。

## 5. 测试数据与隔离

- 使用 `data/raw/insurance/平安福示例条款.md` 验证条款问答与引用；
- 使用 `data/kb/regulations/1129945_保险销售行为管理办法.pdf` 验证真实法规 PDF 解析与入库；
- 使用 `tests/fixtures/insurance-policy.md` 验证合同审查；
- 使用临时目录 `.test-tmp/`，不会修改正式知识库或正式 `data/uploads`；
- 账号测试使用随机用户名，避免相互污染。

## 6. 修改后的最低验证

| 修改内容 | 至少运行 |
|---|---|
| 认证/画像/会话 | `tests/test_auth.py` |
| 解析/分块/向量库 | `tests/test_offline_ingestion.py tests/test_loaders_formats.py tests/test_offline_files_e2e.py` |
| 检索/生成/合规 | `tests/test_retrieval_e2e.py tests/test_compliance.py` |
| 上传/文档审查/图片 | `tests/test_document_review.py tests/test_upload_and_frontend.py tests/test_images.py tests/test_vision.py` |
| 最终交付 | 全量 `pytest` |
