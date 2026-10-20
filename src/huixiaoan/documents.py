from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .compliance.rules import ComplianceChecker
from .config import Config
from .generation.llm import LLMClient
from .generation.prompts import build_context
from .indexing.bm25 import BM25Index
from .ingestion.chunking import StructureChunker
from .ingestion.loaders import SUPPORTED_SUFFIXES, load_document
from .types import AnswerResult, Chunk, Citation, ComplianceResult, IntentResult, SearchHit, UserProfile
from .utils import stable_id

logger = logging.getLogger(__name__)

REVIEW_ASPECTS = [
    ("保险责任与保障范围", ("保险责任", "保障范围", "给付", "赔付", "保险金额")),
    ("责任免除/免责", ("责任免除", "免责", "不承担", "不赔", "除外责任")),
    ("等待期", ("等待期", "观察期")),
    ("犹豫期", ("犹豫期", "冷静期")),
    ("理赔条件与材料", ("理赔", "申请材料", "核定", "保险金申请", "索赔")),
    ("现金价值与退保", ("现金价值", "退保", "解除合同", "退还")),
    ("续保", ("续保", "保证续保", "重新投保")),
    ("保费与费率调整", ("保费", "费率", "保险费调整", "缴费")),
    ("如实告知与核保", ("如实告知", "健康告知", "核保", "既往症")),
    ("争议解决", ("争议", "仲裁", "诉讼", "管辖", "法院")),
    ("特别约定/批注", ("特别约定", "批注", "批单", "补充协议")),
]

REVIEW_QUERIES = [
    "责任免除 免责 不承担 除外责任",
    "等待期 观察期",
    "保险责任 保障范围 给付条件",
    "理赔 申请材料 核定 时限",
    "退保 现金价值 解除合同 损失",
    "续保 保证续保 重新投保",
    "保费 费率调整 缴费",
    "如实告知 健康告知 核保 既往症",
    "争议解决 仲裁 诉讼 管辖",
    "特别约定 批注 批单 补充协议",
]


class DocumentReviewService:
    """上传合同的初步条款审查；不替代律师或持牌顾问的正式意见。"""

    def __init__(
        self,
        cfg: Config,
        llm: LLMClient,
        compliance: ComplianceChecker,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.compliance = compliance
        self.max_bytes = int(cfg.get("documents.max_file_mb", 20)) * 1024 * 1024
        self.upload_dir = cfg.path("data_dir", "data") / "uploads" / "documents"
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def review(
        self,
        filename: str,
        data: bytes,
        question: str = "",
        profile: UserProfile | None = None,
    ) -> AnswerResult:
        started = time.perf_counter()
        profile = profile or UserProfile()
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES - {".csv", ".xlsx"}:
            raise ValueError("合同文件仅支持 PDF、DOCX、Markdown、TXT 或 HTML")
        if not data:
            raise ValueError("文件为空")
        if len(data) > self.max_bytes:
            raise ValueError(f"文件超过 {self.max_bytes // 1024 // 1024} MB")

        safe_name = Path(filename).name or f"contract{suffix}"
        path = self.upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
        path.write_bytes(data)
        doc = load_document(path, self.cfg)
        if any(word in safe_name for word in ("保险", "条款", "保单", "policy")):
            doc.doc_type = "insurance"
            doc.asset_class = "insurance"
        if not doc.text.strip():
            raise ValueError("没有从文件中提取到可用文字；扫描版 PDF 请先进行 OCR")

        chunker = StructureChunker(
            max_chars=int(self.cfg.get("ingestion.profiles.insurance.max_chars", 520)),
            min_chars=int(self.cfg.get("ingestion.profiles.insurance.min_chars", 100)),
            overlap_chars=int(self.cfg.get("ingestion.profiles.insurance.overlap_chars", 100)),
            parent_max_chars=int(self.cfg.get("ingestion.parent_max_chars", 1600)),
            table_max_chars=int(self.cfg.get("ingestion.table_max_chars", 1800)),
        )
        chunks = chunker.chunk_document(doc)
        if not chunks:
            raise ValueError("文件内容过短或无法分块")

        bm25 = BM25Index(
            dict_path=self.cfg.path("domain_dict", "configs/domain_dict.txt")
        ).build(chunks)
        hits = self._collect_hits(bm25, question, chunks)
        context, citations = build_context(
            hits,
            max_chars=int(self.cfg.get("documents.max_context_chars", 12000)),
            parent_expansion=False,
        )

        if str(self.cfg.get("llm.provider", "offline")).lower() == "offline":
            answer = self._rule_review(chunks, citations, question)
        else:
            answer = self._llm_review(question, context, citations, safe_name, chunks)
        answer = ComplianceChecker.ensure_disclaimer(answer)
        compliance = ComplianceResult(
            passed=True,
            action="allow",
            details={"layer": "document_review", "citations": len(citations)},
        )
        return AnswerResult(
            answer=answer,
            citations=citations,
            confidence=0.86,
            intent=IntentResult(
                name="document_review",
                confidence=0.9,
                entities={"filename": safe_name, "chunks": len(chunks)},
                rationale="uploaded_contract_review",
            ),
            strategy="document_review",
            compliance=compliance,
            latency_ms=(time.perf_counter() - started) * 1000,
            model=self.llm.model_name,
            visual={
                "document": {
                    "filename": safe_name,
                    "doc_id": doc.doc_id,
                    "chunks": len(chunks),
                }
            },
            debug={"review_aspects": [item[0] for item in REVIEW_ASPECTS]},
        )

    def _collect_hits(
        self, bm25: BM25Index, question: str, chunks: list[Chunk]
    ) -> list[SearchHit]:
        queries = [question] if question.strip() else []
        queries.extend(REVIEW_QUERIES)
        merged: dict[str, SearchHit] = {}
        for query in queries:
            for hit in bm25.search(query, top_k=4):
                merged.setdefault(hit.chunk.chunk_id, hit)
        if not merged:
            # 没有关键词命中时，给审查模型一个可用的最小上下文。
            return [
                SearchHit(chunk=chunk, score=0.0, retriever="fallback", rank=index + 1)
                for index, chunk in enumerate(chunks[:8])
            ]
        hits = sorted(merged.values(), key=lambda item: item.score, reverse=True)
        # 保证审查覆盖不同条款模块，而不是只取同一段。
        return hits[:20]

    def _rule_review(
        self, chunks: list[Chunk], citations: list[Citation], question: str
    ) -> str:
        text = "\n".join(chunk.text for chunk in chunks)
        lines = [
            "我做了一次基于条款文本的初步审查。以下内容用于发现风险和待核对项，"
            "不构成正式法律意见，也不能替代律师或持牌保险顾问对合同效力、承保条件和理赔结果的审核。",
            "",
            "一、初步结论",
        ]
        present = []
        missing = []
        for label, keywords in REVIEW_ASPECTS:
            if any(keyword in text for keyword in keywords):
                present.append(label)
            else:
                missing.append(label)
        lines.append(
            f"文件中可识别到 {len(present)}/{len(REVIEW_ASPECTS)} 个关键条款模块。"
            "仅凭文件文本无法判断合同在法律上是否“可行”，还需要核对投保资格、健康告知、"
            "承保结论、缴费能力和监管要求。"
        )
        if missing:
            lines.append("建议重点确认缺失或表述不明显的模块：" + "、".join(missing) + "。")
        lines.extend(["", "二、关键条款摘录"])
        for citation in citations[:8]:
            quote = re.sub(r"^#+\s*", "", citation.quote)
            lines.append(
                f"- [{citation.index}] {citation.article or citation.section or citation.title}："
                f"{quote[:180]}"
            )
        lines.extend(["", "三、需要重点核对的风险点"])
        risk_keywords = [
            ("等待期", "等待期内发生的事故可能不承担责任"),
            ("责任免除", "免责情形可能排除部分保障"),
            ("不承担", "条款中存在明确的不承担责任情形"),
            ("免赔额", "理赔前需要先扣除免赔额"),
            ("不保证续保", "续保存在不确定性"),
            ("现金价值", "退保/解约可能产生损失"),
            ("费率", "费率或保费调整会影响长期成本"),
            ("如实告知", "健康告知不完整可能影响承保或理赔"),
            ("争议", "争议解决和管辖条款影响维权成本"),
        ]
        flagged = 0
        for keyword, message in risk_keywords:
            if keyword in text:
                lines.append(f"- {message}（条款中出现“{keyword}”）。")
                flagged += 1
        if not flagged:
            lines.append("- 未识别到明确的免责、等待期或退保损失条款，请确认文件是否完整。")
        lines.extend(
            [
                "",
                "四、签署/投保前建议",
                "- 核对合同是否包含完整的保险责任、责任免除、等待期、理赔材料、退保和争议解决条款。",
                "- 对缺失、空白、手写补充或表述模糊的条款要求保险公司书面确认。",
                "- 将产品风险等级与投保人/被保险人实际情况做适当性匹配，不要仅依据销售演示做决定。",
                "- 重要数字（保额、免赔额、费率、等待期）逐项与合同原文核对。",
                "",
                "如果你希望进一步判断，请补充：投保人年龄/职业、保障目标、预算、健康告知情况，"
                "以及你重点关注的责任或理赔场景。",
            ]
        )
        return "\n".join(lines)

    def _llm_review(
        self,
        question: str,
        context: str,
        citations: list[Citation],
        filename: str,
        chunks: list[Chunk],
    ) -> str:
        prompt = f"""你是金融合同条款审查助手。请基于用户上传文件做初步风险审查，不提供正式法律意见。

文件：{filename}
用户关注：{question or "请检查文件是否存在漏洞、风险，以及合同是否具备进一步审阅/签约的条件。"}

【文件条款】
{context}

请按以下结构输出：
1. 初步结论：说明仅凭文本无法做法律效力/承保结果的最终判断；
2. 主要风险与潜在漏洞：逐条列出，并用 [编号] 引用原文；
3. 缺失或表述不清的条款；
4. 签约/投保前建议；
5. 必要时向用户追问投保人、预算、健康告知和保障目标。
不要承诺收益、不要绝对化判断合同一定有效/无效，不要编造文件中没有的条款。"""
        try:
            answer = self.llm.complete(
                [
                    {"role": "system", "content": "你是严谨的金融合同条款审查助手，只依据提供的文件回答。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1200,
            )
            if not answer.strip():
                return self._rule_review(chunks, citations, question)
            return answer
        except Exception as exc:
            logger.warning("合同审查模型调用失败，回退规则审查: %s", exc)
            return self._rule_review(chunks, citations, question)
