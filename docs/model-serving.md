# 模型服务与轻量化

## 1. 两种接入方式

本项目的生成侧统一走 OpenAI Chat Completions 兼容接口：

- **DeepSeek 官方 API**：`base_url=https://api.deepseek.com/v1`，`model=deepseek-chat`；
- **自托管 Qwen2.5-14B + vLLM**：`base_url=http://host:8001/v1`，`model=Qwen2.5-14B-Instruct`；
- **离线抽取式**：`llm.provider=offline`，用于本地开发和 CI。

业务代码只依赖 `LLMClient`，切换模型不需要改 RAG 链路。

## 2. vLLM 启动示例

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct \
  --served-model-name Qwen2.5-14B-Instruct \
  --host 0.0.0.0 --port 8001 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs 64 \
  --enable-prefix-caching
```

LoRA 适配器：

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct \
  --served-model-name Qwen2.5-14B-Instruct \
  --enable-lora \
  --lora-modules finance-lora=/models/finance-lora \
  --max-lora-rank 32
```

请求时把 `model` 设为 `finance-lora` 即可。

## 3. 量化

| 方案 | 适用 | 注意 |
|---|---|---|
| AWQ / GPTQ 4bit | 显存受限、吞吐优先 | 必须重跑准确率/Faithfulness/拒答率 |
| FP8 | H100/H20 等新卡 | 依赖 vLLM 与显卡支持 |
| Tensor Parallel | 14B/32B 多卡 | 增加通信开销，调整 `tensor-parallel-size` |

量化不是“免费加速”。必须做 A/B：

1. 同一评测集、同一 Prompt、同一知识库；
2. 比较答案正确率、引用一致性、幻觉率、拒答率；
3. 再比较首 token 延迟、tokens/s、P95、显存；
4. 只有质量下降在可接受范围内才上线。

## 4. LoRA/SFT 数据

推荐样本结构：

```json
{
  "messages": [
    {"role": "system", "content": "你是小智财答理财顾问，只依据资料回答并引用。"},
    {"role": "user", "content": "【知识库资料】[1] ...\n【问题】消费板块现在值得配置吗？"},
    {"role": "assistant", "content": "等待期为90日 [1]。\n\n注意事项：..."}
  ]
}
```

训练目标应聚焦：

- 回答格式（结论 → 依据 → 风险提示）；
- 引用习惯；
- 资料不足时的拒答；
- 合规表达（不承诺收益、不绝对化）；
- 金融术语的规范性。

不要把检索上下文当作模型参数知识反复训练。知识更新应走索引，而不是重新 SFT；否则模型会“记住旧条款”，RAG 的时效性优势被抵消。

建议超参起点（14B LoRA）：

```text
r=16, alpha=32, dropout=0.05
learning_rate=1e-4, epochs=2, warmup_ratio=0.03
max_seq_len=4096, bf16, gradient_checkpointing=true
```

## 5. 上线门禁

- 基座 + RAG vs LoRA + RAG 的对照评测；
- 合规用例集：收益承诺、绝对化、适当性、拒答；
- 引用一致性：答案中的 `[n]` 必须存在且支持结论；
- 量化前后延迟与质量对比；
- 线上灰度 5% → 20% → 100%，观察投诉与合规拦截率。
