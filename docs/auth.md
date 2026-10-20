# 账号、登录与身份隔离

## 1. 设计目标

金融问答必须满足“谁在问、以什么风险等级问、回答给谁看”三件事。项目采用本地账号体系：

```text
用户名 / 密码
  → PBKDF2-SHA256 加盐哈希（310,000 次迭代）
  → HMAC-SHA256 签名 Token（默认 8 小时）
  → 服务端解析 user_id 与风险画像
  → 会话记忆 / 审计 / 产品适当性过滤
```

客户端传入的 `profile.user_id` 和 `profile.risk_level` 在登录状态下会被服务端覆盖，不能通过改请求体冒充他人或抬高风险等级。

## 2. 接口

### 注册

`POST /v1/auth/register`

```json
{
  "username": "advisor001",
  "password": "FinTest2026",
  "display_name": "王顾问"
}
```

用户名 3-32 位，支持中文、字母、数字、下划线、短横线；密码至少 8 位，且同时包含字母和数字。

返回：

```json
{
  "access_token": "v1....",
  "token_type": "bearer",
  "expires_in": 28800,
  "user": {
    "id": "u_...",
    "username": "advisor001",
    "display_name": "王顾问",
    "risk_level": "C3"
  }
}
```

### 登录

`POST /v1/auth/login`，请求体同注册（不需要 `display_name`）。

错误密码与不存在的用户名统一返回 `401 用户名或密码错误`，避免账号枚举。

### 当前用户

`GET /v1/auth/me`

```http
Authorization: Bearer <access_token>
```

### 更新画像

`PATCH /v1/auth/profile`

```json
{"risk_level": "C3", "investment_experience": "3年", "horizon": "中长期", "investable_assets": 1000000}
```

`POST /v1/profile/assess` 在登录状态下会把 10 题问卷结果写回账号画像。

## 3. 会话与审计隔离

前端仍使用本地生成的 `session_id`，服务端会加上账号前缀：

```text
匿名：anon:<session_id>
登录：u:<user_id>:<session_id>
```

因此两个用户即使都使用 `session-1234`，也不会共享对话记忆；审计记录中的 `user_id` 同样来自 Token，而不是请求体。

## 4. 配置

```yaml
security:
  token_secret: "${HUIXIAOAN_TOKEN_SECRET:-}"
  token_ttl_hours: 8
  login_rate_per_minute: 10
  require_auth_for_chat: false
  trust_proxy_headers: false
```

- `token_secret`：生产多实例必须配置相同值；留空时自动生成到 `auth_db` 旁的 `.secret` 文件。
- `token_ttl_hours`：Token 有效期。
- `login_rate_per_minute`：按 IP 限制登录/注册尝试。
- `require_auth_for_chat`：设为 `true` 后，问答、流式、文件审查和 WebSocket 必须登录；默认 `false` 保留匿名体验。
- `trust_proxy_headers`：仅当服务位于可信 Nginx/网关之后时设为 `true`，否则不信任客户端伪造的 `X-Forwarded-For`。

环境变量：

```powershell
$env:HUIXIAOAN_TOKEN_SECRET = "replace-with-a-long-random-secret"
$env:HUIXIAOAN_AUTH_DB = "mysql://user:pass@127.0.0.1:3306/huixiaoan"
```

不配置 `HUIXIAOAN_AUTH_DB` 时使用 `data/audit/auth.db`（SQLite），适合本地开发；生产切换 MySQL 即可。

## 5. 安全边界

已实现：

- PBKDF2 加盐哈希，数据库不保存明文密码；
- Token 签名校验、过期校验、用户禁用/版本校验；
- 登录/注册按 IP 限流；
- 统一登录失败文案，降低账号枚举风险；
- 服务端覆盖身份和风险等级；
- 会话按账号隔离；
- 审计记录绑定真实 `user_id`。

尚未实现（生产接入平台账号体系时补充）：

- 邮箱/短信验证码、找回密码；
- OAuth2/OIDC/企业 SSO；
- Token 主动吊销列表（当前退出仅清理浏览器 Token）；
- 多因素认证与设备管理。

## 6. 测试

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m pytest -q tests/test_auth.py tests/test_upload_and_frontend.py
```

覆盖注册、重复用户名、弱密码、错误密码、Token 过期、伪造身份、画像持久化、会话隔离、流式鉴权、WebSocket 非法 Token 和登录限流。
