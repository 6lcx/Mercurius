# -*- coding: utf-8 -*-
"""category_knowledge

品类洞察 RAG 知识库：复用 AgentScope 2.0 的 KnowledgeBase + QdrantStore + OpenAIEmbeddingModel。

与商品向量索引分开两套 collection：
    globex_products     商品卡向量（模块一：二阶段召回）
    globex_category_kb  品类洞察知识（本模块：RAG 问答）

建库流程：TextParser 读 knowledge/*.md → ApproxTokenChunker 切块 → insert_document（按文件名做 document_id，幂等）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agentscope.rag import ApproxTokenChunker, KnowledgeBase, QdrantStore, TextParser

from app.infrastructure.embedding.factory import build_rag_embedding_model
from app.infrastructure.settings import PROJECT_ROOT, Settings
from app.infrastructure.rag.web_vector_store import WebQdrantStore

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"

_KB_DESCRIPTION = (
    "Mercurius 跨境电商品类洞察知识库：各品类的热卖款型、关键属性判断口径、"
    "价格区间参考、避坑点，以及跨境到手价/免税额度/合规通则。"
)


def build_category_knowledge_base(settings: Settings) -> KnowledgeBase:
    return _build_knowledge_base(settings, web=False)


def build_web_knowledge_base(settings: Settings) -> KnowledgeBase:
    """隔离存储低可信网页，复用同一 embedding 模型与相似度。"""
    if settings.web_kb_collection in (settings.category_kb_collection, settings.qdrant_collection):
        raise ValueError("网页 collection 必须独立于本地知识库和商品索引")
    return _build_knowledge_base(settings, web=True)


def _build_knowledge_base(settings: Settings, *, web: bool) -> KnowledgeBase:
    """构建品类知识库对象（不建库，建库见 bootstrap_category_knowledge）。"""
    embedding_model = build_rag_embedding_model(settings)
    store_class = WebQdrantStore if web else QdrantStore
    if settings.qdrant_url:
        vector_store = store_class(url=settings.qdrant_url, distance="Cosine")
    else:
        local_path = settings.data_dir / ("qdrant_web_kb" if web else "qdrant_kb")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        vector_store = store_class(path=str(local_path), distance="Cosine")
    return KnowledgeBase(
        name="web_insight" if web else "category_insight",
        description="低可信外部网页证据，仅作相关信息参考，不代表合规认证。" if web else _KB_DESCRIPTION,
        embedding_model=embedding_model,
        vector_store=vector_store,
        collection=settings.web_kb_collection if web else settings.category_kb_collection,
    )


async def bootstrap_category_knowledge(
    knowledge_base: KnowledgeBase,
    knowledge_dir: Optional[Path] = None,
) -> int:
    """把 knowledge/*.md 灌入知识库（幂等），返回入库文档数；失败仅告警返回 0。"""
    directory = knowledge_dir or KNOWLEDGE_DIR
    try:
        await knowledge_base.ensure_collection()
        existing = {doc.document_id for doc in await knowledge_base.list_documents()}
        parser, chunker = TextParser(), ApproxTokenChunker(chunk_size=512, overlap=50)
        inserted = 0
        for md_file in sorted(directory.glob("*.md")):
            document_id = md_file.stem
            if document_id in existing:
                continue
            sections = await parser.parse(str(md_file), filename=md_file.name)
            chunks = await chunker.chunk(sections)
            await knowledge_base.insert_document(
                chunks=chunks,
                document_id=document_id,
                document_metadata={"source": md_file.name},
            )
            inserted += 1
        logger.info(
            "品类知识库就绪：新增 %d 篇，累计 %d 篇",
            inserted,
            len(existing) + inserted,
        )
        return inserted
    except Exception as err:  # noqa: BLE001 —— 知识库不可用不阻塞启动
        logger.warning("品类知识库建库失败，category_insight 将不可用：%s", err)
        return 0
