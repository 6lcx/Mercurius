"""Opt-in live RAG verification; isolated stores, no credentials in the report."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.application.usecases.web_knowledge import WebKnowledgeService
from app.infrastructure.rag.category_knowledge import (
    bootstrap_category_knowledge, build_category_knowledge_base, build_web_knowledge_base,
)
from app.infrastructure.rag.web_source import TavilyWebSource
from app.infrastructure.settings import load_settings
from app.application.usecases.web_product_policy import prepare_product_page

DEFAULT_QUESTION = "露营灯的 IPX4 和 IPX7 防水等级有什么区别，雨天露营该怎么选？"


class CapturedSource:
    def __init__(self, source):
        self.source = source
        self.pages = []
        self.replay = False
        self.error = None

    async def search(self, query, max_results=5):
        if not self.replay:
            try:
                self.pages = await self.source.search(query, max_results=max_results)
            except Exception as exc:
                self.error = {"type": type(exc).__name__}
                if getattr(exc, "status_code", None) is not None:
                    self.error["http_status"] = exc.status_code
                response = getattr(exc, "response", None)
                if response is not None:
                    self.error["http_status"] = response.status_code
                raise
        return self.pages


def public_result(result):
    output = {key: result[key] for key in ("gate", "admitted_count", "web_status", "discovery_status", "storage_status", "product_status") if key in result}
    output["insights"] = []
    for row in result.get("insights", []):
        safe = {key: row[key] for key in ("document_id", "source_type", "source", "url", "title", "score", "raw_score", "weight", "content_hash") if key in row}
        if row.get("source_type") == "web":
            safe["accepted_excerpt"] = row.get("content", "")[:200]
        output["insights"].append(safe)
    return output


CURRENT_STAGE = None


async def stage(name, action):
    global CURRENT_STAGE
    CURRENT_STAGE = name
    print(f"stage: {name}", flush=True)
    return await asyncio.wait_for(action, timeout=240)


async def close_kb(kb):
    if kb is not None:
        await kb.vector_store.__aexit__(None, None, None)


async def run(args, report, run_dir):
    settings = load_settings()
    if not args.prepare_only and not settings.tavily_api_key:
        report["status"] = "blocked_missing_tavily"
        report["configuration_missing_tavily"] = True
        return 2
    settings = replace(settings, data_dir=run_dir, qdrant_url="", web_knowledge_weight=.8)
    report["model"] = settings.embedding_model
    local, web = None, None
    try:
        local = build_category_knowledge_base(settings)
        web = build_web_knowledge_base(settings)
        # The bootstrap helper deliberately swallows exceptions for app startup.
        # Probe directly first so real provider HTTP failures remain diagnosable.
        await stage("embedding_preflight", local.embedding_model([args.question]))
        inserted = await stage("embed_local_knowledge", bootstrap_category_knowledge(local))
        report["local_documents_inserted"] = inserted
        baseline = await stage("local_top_k", local.search(queries=[args.question], top_k=3))
        report["local_baseline"] = [{"document_id": row.document_id, "source": (row.chunk.metadata or {}).get("source", row.document_id), "score": row.score} for row in baseline]
        if not baseline or inserted == 0:
            report["status"] = "failed_local_baseline"
            return 1
        if args.prepare_only:
            report["status"] = "prepared_only_web_not_tested"
            return 0
        source = CapturedSource(TavilyWebSource(settings))
        service = WebKnowledgeService(local, web, source, weight=.8)
        result = await stage("live_search_extract_score_admit", service.search(args.question, top_k=3, discover=True))
        report["provider_pages"] = [{"url": p["url"], "title": p["title"], "body_characters": len(p["content"])} for p in source.pages]
        (run_dir / "provider_pages.json").write_text(json.dumps(source.pages, ensure_ascii=False), encoding="utf-8")
        report["first_live_result"] = public_result(result)
        if source.error:
            report["provider_error"] = source.error
        if result.get("discovery_status") or result.get("storage_status") or result.get("gate", {}).get("status") != "active":
            report["status"] = "failed_live_stage"
            return 1
        accepted_ids = {r["document_id"] for r in result["insights"] if r["source_type"] == "web"}
        accepted_rows = [r for r in result["insights"] if r["source_type"] == "web"]
        if args.require_products:
            report["product_acceptance_verified"] = bool(accepted_rows) and all(
                prepare_product_page(row, args.question) is not None for row in accepted_rows
            )
            if not report["product_acceptance_verified"]:
                report["status"] = "failed_product_acceptance"
                return 1
        threshold = result["gate"]["threshold"]
        report["weighted_gate_verified"] = all(
            row["score"] > threshold and math.isclose(
                row["score"], .8 * max(0.0, row["raw_score"]), rel_tol=1e-10, abs_tol=1e-12,
            ) for row in accepted_rows
        )
        stored_documents = await stage("verify_only_accepted_documents_persisted", web.list_documents())
        stored_ids = {document.document_id for document in stored_documents}
        report["stored_document_ids"] = sorted(stored_ids)
        report["only_accepted_documents_persisted"] = stored_ids == accepted_ids
        if not report["weighted_gate_verified"] or stored_ids != accepted_ids:
            report["status"] = "failed_admission_invariant"
            return 1
        if not source.pages:
            report["status"] = "completed_no_provider_pages_admission_not_verified"
            return 0
        if not accepted_ids:
            report["status"] = "gate_passed_no_admission"
            return 0
        source.replay = True
        repeated = await stage("repeat_captured_real_pages_no_second_search_request", service.search(args.question, top_k=3, discover=True))
        report["repeat_source_mode"] = "in_memory_replay_of_first_real_provider_response"
        report["duplicate_result"] = public_result(repeated)
        if repeated.get("admitted_count") != 0 or repeated.get("storage_status") or repeated.get("discovery_status"):
            report["status"] = "failed_duplicate_check"
            return 1
        await close_kb(local)
        local = None
        await close_kb(web)
        web = None
        local, web = build_category_knowledge_base(settings), build_web_knowledge_base(settings)
        reopened = await stage("reopen_persistent_qdrant_recall", WebKnowledgeService(local, web, None, weight=.8).search(args.question, top_k=3, discover=False))
        report["reopened_result"] = public_result(reopened)
        recalled_ids = {r["document_id"] for r in reopened["insights"] if r["source_type"] == "web"}
        report["status"] = "passed_live_admission_dedup_persistence" if accepted_ids <= recalled_ids else "failed_persistent_recall"
        return 0 if report["status"].startswith("passed_") else 1
    finally:
        await close_kb(local)
        await close_kb(web)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--require-products", action="store_true", help="Require at least one admitted purchase page; reject article-only or empty results")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "data" / "verify_web_live" / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {"started_at": stamp, "question": args.question, "weight": .8, "top_k": 3, "prepare_only": args.prepare_only, "isolated_data_dir": str(run_dir)}
    try:
        code = asyncio.run(run(args, report, run_dir))
    except Exception as exc:
        report["status"] = "failed_exception"
        report["error_type"] = type(exc).__name__
        report["failed_stage"] = CURRENT_STAGE
        if getattr(exc, "status_code", None) is not None:
            report["http_status"] = exc.status_code
        response = getattr(exc, "response", None)
        if response is not None:
            report["http_status"] = response.status_code
        code = 1
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"status: {report['status']}", flush=True)
    print(f"report: {report_path}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
