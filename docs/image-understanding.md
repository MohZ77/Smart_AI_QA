# 用户图片识别

## 1. 能力

用户可以在工作台点击「图片」，上传 PNG/JPEG/WEBP/GIF/BMP，系统会：

1. 校验图片格式、大小（默认最大 8 MB）；
2. 落盘到 `data/images/user-uploads/`，通过 `/v1/images/{id}` 回显原图；
3. 调用 OpenAI 兼容的视觉模型识别图片文字和内容；
4. 把识别文本加入 RAG 检索与 Prompt；
5. 返回 `visual` 字段（识别结果、模型、错误信息）和带引用的回答。

未配置视觉模型时，系统会明确返回“图片识别失败”，不会编造图片内容。

## 2. 配置视觉模型

### Qwen-VL / DashScope

```powershell
$env:HUIXIAOAN_VISION_PROVIDER = "qwen-vl"
$env:HUIXIAOAN_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:HUIXIAOAN_VISION_MODEL = "qwen-vl-max"
$env:HUIXIAOAN_VISION_API_KEY = "sk-..."
```

### 本地 vLLM Qwen2.5-VL

```bash
vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
  --host 0.0.0.0 --port 8002 \
  --max-model-len 8192
```

```powershell
$env:HUIXIAOAN_VISION_PROVIDER = "openai"
$env:HUIXIAOAN_VISION_BASE_URL = "http://127.0.0.1:8002/v1"
$env:HUIXIAOAN_VISION_MODEL = "Qwen2.5-VL-7B-Instruct"
$env:HUIXIAOAN_VISION_API_KEY = "EMPTY"
```

## 3. API 调用

`POST /v1/chat` 和 `/v1/chat/stream` 支持 `image_base64`、`image_mime`：

```json
{
  "question": "这张图片里的产品风险等级是什么？",
  "session_id": "s1",
  "profile": {"user_id": "u1", "risk_level": "C3"},
  "image_base64": "iVBORw0KGgo...",
  "image_mime": "image/png"
}
```

响应额外包含：

```json
{
  "visual": {
    "ok": true,
    "text": "图片识别出的文字……",
    "provider": "openai-compatible",
    "error": "",
    "image": {"id": "...", "url": "/v1/images/..."}
  }
}
```

## 4. 识别结果如何参与 RAG

识别文本会以如下形式加入问题：

```text
【用户上传图片识别内容】
……
```

随后照常执行意图识别、缓存、Router、混合检索、Rerank、生成和合规审查。识别文本只是检索线索，最终结论仍必须引用知识库资料。

## 5. 安全与限制

- 图片大小默认 8 MB，格式按魔数校验；
- 图片只通过索引 ID 访问，不接受任意路径；
- 视觉模型调用失败或未配置时直接拒答；
- 图片识别可能受清晰度、旋转、表格版式影响，重要结论应回到原文核对；
- 用户上传图片和识别文本应按企业数据安全要求设置保留期限。
