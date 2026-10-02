"""Measure the production knowledge retriever in an isolated local collection.

Document recall after top-3 chunk retrieval matches category_insight_tool's
default. This does not measure generated-answer correctness or freshness.
"""
import argparse
import asyncio
from dataclasses import asdict, replace
from pathlib import Path

from app.infrastructure.rag.category_knowledge import (
    build_category_knowledge_base, bootstrap_category_knowledge, KNOWLEDGE_DIR,
)
from scripts.eval.run_category_recall import load_dataset, run_dataset
from .common import digest, write_json
from .live import base_settings


async def run(output: Path):
    if output.exists():
        raise ValueError('Use a fresh output directory to preserve evidence')
    output.mkdir(parents=True)
    dataset = Path('eval/category_recall.jsonl')
    cases = load_dataset(dataset)
    write_json(output / 'protocol.json', {
        'scope': 'existing development labels; document recall from top-3 chunks',
        'cases': cases, 'k_chunks': 3, 'paid_api_calls': 0,
        'dataset_sha256': digest(dataset),
        'knowledge_sha256': {p.name: digest(p) for p in KNOWLEDGE_DIR.glob('*.md')},
        'limitation': 'No answer-generation, citation-entailment or freshness evaluation',
    })
    kb = build_category_knowledge_base(replace(base_settings(), data_dir=output / 'state'))
    try:
        inserted = await bootstrap_category_knowledge(kb)
        if not inserted:
            raise RuntimeError('Fresh knowledge collection did not ingest documents')
        result = await run_dataset(kb, cases, top_k=3)
        write_json(output / 'results.json', asdict(result))
        print({'documents': inserted, 'cases': result.count, 'recall': result.recall,
               'mrr': result.mrr, 'ndcg': result.ndcg}, flush=True)
    finally:
        await kb.vector_store.__aexit__(None, None, None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    asyncio.run(run(parser.parse_args().output))
