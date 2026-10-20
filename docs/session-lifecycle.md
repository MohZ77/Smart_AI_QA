# 会话生命周期：从“点一下就新建”到可落地的会话管理

## 1. 问题根因

旧实现把“点击新建”直接当成“创建会话”：

```text
点击新会话 → 立即生成 session_id → 立即写入 localStorage → 左侧出现 0 轮记录
```

用户连续点击、刷新页面或清空后重新进入，都会留下大量空会话。空会话没有业务价值，还会带来三个问题：

- 左侧历史被噪声占满，真实会话难以找到；
- 每个空会话都可能被误当成服务端上下文，增加无效查询；
- 多端同步时无法区分“用户真的创建了会话”和“只是点了一下按钮”。

## 2. 当前实现

前端采用**草稿会话 + 提交会话**两阶段模型：

```text
点击「新会话」
  → 只创建内存草稿（draft），不写历史、不生成后端消息

发送第一条用户消息
  → 草稿提交为正式会话
  → 标题取第一条问题（截断）
  → 写入历史列表

点击「新会话」时当前仍是 0 轮草稿
  → 复用/重置当前草稿，不再新增记录

点击「清空当前」
  → 从本地历史移除
  → 调用 DELETE /v1/sessions/{session_id}
  → 清理服务端会话记忆
```

具体约束：

- `0` 轮会话不进入 `localStorage`，也不出现在左侧；
- 历史列表只渲染 `messages.length > 0` 的会话；
- 首次发送问题前，会话标题保持“新会话”且不落库；
- 首次发送后，按第一条用户问题生成标题；
- 同一账号的会话按 `u:<user_id>:<session_id>` 隔离；
- 清空操作幂等，重复调用不会报错；
- 审计记录不随会话清空删除，满足合规追溯要求。

## 3. 生产级会话表设计

当前实现把历史保存在浏览器；生产建议把服务端作为会话事实源：

```sql
CREATE TABLE chat_session (
  id              VARCHAR(64) PRIMARY KEY,
  user_id         VARCHAR(64) NOT NULL,
  client_key      VARCHAR(96) NOT NULL,
  title           VARCHAR(128) NOT NULL DEFAULT '新会话',
  status          VARCHAR(16) NOT NULL DEFAULT 'active',
  message_count   INT NOT NULL DEFAULT 0,
  version         INT NOT NULL DEFAULT 0,
  created_at      DATETIME NOT NULL,
  updated_at      DATETIME NOT NULL,
  last_message_at DATETIME NULL,
  deleted_at      DATETIME NULL,
  UNIQUE KEY uk_user_client (user_id, client_key),
  KEY idx_user_updated (user_id, deleted_at, updated_at)
);

CREATE TABLE chat_message (
  id          VARCHAR(64) PRIMARY KEY,
  session_id  VARCHAR(64) NOT NULL,
  role        VARCHAR(16) NOT NULL,
  content     MEDIUMTEXT NOT NULL,
  citations   JSON NULL,
  request_id  VARCHAR(64) NULL,
  created_at  DATETIME NOT NULL,
  KEY idx_session_created (session_id, created_at)
);
```

关键点：

1. **首次用户消息才创建 `chat_session`**，不是按钮点击时创建。
2. 客户端生成 `client_key`，服务端用 `(user_id, client_key)` 做唯一约束；重试使用 `INSERT ... ON DUPLICATE KEY UPDATE`，保证幂等。
3. `title` 先用首条问题截断；超过 3 轮后可异步用 LLM 生成更准确的标题，但要允许用户手动改名。
4. 列表查询固定条件：

```sql
WHERE user_id = ?
  AND deleted_at IS NULL
  AND message_count > 0
ORDER BY updated_at DESC
LIMIT 30
```

5. 多端同步以服务端列表为准，`localStorage` 只做首屏缓存，不作为事实源。
6. 清空采用软删除：`deleted_at = NOW()`，同时清理会话记忆；审计表保留，满足金融合规审计要求。
7. 删除接口幂等：不存在或已删除也返回成功，避免客户端重试造成报错。

## 4. 分页、并发与容量

### 分页

不要使用大偏移量 `OFFSET`。使用游标：

```sql
WHERE user_id = ?
  AND deleted_at IS NULL
  AND message_count > 0
  AND (updated_at, id) < (?, ?)
ORDER BY updated_at DESC, id DESC
LIMIT 30
```

### 并发

- 同一 `client_key` 的首次消息可能并发到达：唯一约束 + upsert 保证只创建一个会话；
- 标题自动生成使用 `version` 乐观锁，避免覆盖用户手动改名；
- 清空与发送并发时，先写消息再检查 `deleted_at`，或使用会话级短锁；已删除会话拒绝追加消息。

### 数据增长

- 会话按 `user_id` 哈希分片，消息表按月分区；
- 列表只查 `chat_session` 汇总表，不扫描消息明细；
- LLM 上下文只取最近 5 轮 + 历史摘要，不把全部消息塞进 Prompt；
- 旧消息归档到对象存储，数据库保留摘要和引用元数据；
- 空草稿永不入库；如果产品确实需要服务端草稿，设置 24 小时 TTL 并由定时任务清理。

## 5. 监控指标

建议增加：

```text
session_draft_created_total
session_committed_total
empty_session_filtered_total
session_list_latency_ms
session_clear_total
session_clear_failures_total
```

其中 `empty_session_filtered_total` 可以直接反映前端是否又在制造空会话。

## 6. 当前代码位置

- 前端草稿/提交逻辑：`web/index.html`
- 服务端清理接口：`DELETE /v1/sessions/{session_id}`，见 `src/huixiaoan/service/app.py`
- 会话记忆存储：`src/huixiaoan/memory.py`
- 回归测试：`tests/test_auth.py::test_delete_session_clears_server_side_memory`

## 7. 后续演进

当前实现仍以浏览器 `localStorage` 保存完整消息和引用；生产接入时建议：

1. 新增 `chat_session` / `chat_message` 表；
2. 前端改为 `GET /v1/sessions` 拉取列表、`GET /v1/sessions/{id}/messages` 拉取消息；
3. 引用、反馈、审计与消息 ID 绑定；
4. 增加重命名、置顶、归档、软删除和跨设备同步；
5. 为会话列表和消息读取增加用户级权限校验与审计。
