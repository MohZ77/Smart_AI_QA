# 示例语料说明

本目录下的保险条款、产品说明书、研报与监管材料均为**虚构的演示文本**，
仅用于验证 RAG 链路的解析、分块、召回、引用与合规审查，不构成任何真实产品的
条款、费率、收益或理赔承诺。

接入生产环境时，请将真实文档放入对应子目录，并在元数据中补充：

- `product_code`：产品编码
- `effective_date`：条款生效日期
- `risk_level`：产品风险等级（R1-R5）
- `doc_type`：insurance / fund / research / regulation / faq
