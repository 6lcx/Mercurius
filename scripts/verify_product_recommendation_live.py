"""Opt-in real product discovery/service verification, with isolated catalog writes."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.application.usecases.product_recommendation import ProductRecommendationService
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.catalog.external_discovery import ExternalProductDiscovery
from app.infrastructure.embedding.factory import build_embedding_client
from app.infrastructure.persistence.persistent_product_repository import PersistentProductRepository
from app.infrastructure.settings import load_settings

DEFAULT_QUERY = "推荐适合双人露营的可充电露营灯，充电方便、续航好，给我真实购买链接"


class CapturedDiscovery:
    def __init__(self, source):
        self.source = source
        self.calls = 0
        self.products = []
        self.replay = False

    async def discover(self, spec):
        if not self.replay:
            self.calls += 1
            self.products = await self.source.discover(spec)
        return self.products


async def run(args, report, directory):
    settings = load_settings()
    if not settings.tavily_api_key:
        report["status"] = "blocked_missing_tavily"
        return 2
    repository = PersistentProductRepository(directory)
    seed_ids = {p.product_id for p in await repository.list_all()}
    discovery = CapturedDiscovery(ExternalProductDiscovery(settings))
    embedder = build_embedding_client(settings)
    service = ProductRecommendationService(repository, discovery=discovery, embedder=embedder,
                                           cache_path=directory / "recommendation_cache.json")
    spec = ProductSearchSpec(normalized_query=args.query, top_k=3)
    report["query"] = args.query
    report["embedding_model"] = settings.embedding_model
    report["scope"] = "Real Tavily + local embedding + product recommendation service; not a full LLM Agent conversation"
    print("stage: real_generic_product_recommendation", flush=True)
    first = await asyncio.wait_for(service.execute(spec, session_key="live-demo"), timeout=240)
    report["first"] = first
    report["provider_pages"] = getattr(discovery.source, "last_pages", [])
    report["captured_products"] = [asdict(p) for p in discovery.products]
    calls_before = discovery.calls
    print("stage: repeat_same_demand", flush=True)
    second = await service.execute(spec, session_key="live-demo")
    report["repeat"] = second
    report["repeat_no_discovery"] = discovery.calls == calls_before
    report["repeat_same_products"] = [h["product_id"] for h in first.get("hits", [])] == [h["product_id"] for h in second.get("hits", [])]
    reopened = PersistentProductRepository(directory)
    products = await reopened.list_all()
    current_ids = {p.product_id for p in products}
    added = [p for p in products if p.product_id not in seed_ids]
    report["seed_products_retained"] = seed_ids <= current_ids
    report["persisted_external_products"] = [asdict(p) for p in added]
    report["admitted_survive_reopen"] = bool(first.get("admitted_ids")) and set(first["admitted_ids"]) <= current_ids
    # Reuse the exact captured products: budget diagnostics do not spend another search.
    discovery.replay = True
    print("stage: budget_unknown_fee_diagnostic_replayed_sources", flush=True)
    budget_service = ProductRecommendationService(PersistentProductRepository(directory / "budget"),
                                                  discovery=discovery, embedder=embedder)
    budget_spec = replace(spec, ship_to="CN", price_max_major=300, target_currency="CNY")
    report["budget_300_cny_replay"] = await budget_service.execute(budget_spec, session_key="budget-demo")
    report["live_discovery_calls"] = discovery.calls
    passed = all((added, report["seed_products_retained"], report["admitted_survive_reopen"],
                  report["repeat_no_discovery"], report["repeat_same_products"],
                  all(p.purchase_url.startswith("https://") for p in added)))
    report["status"] = "passed_live_product_admission_cache_persistence" if passed else "incomplete_no_admission_or_failed_invariant"
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize real Tavily search; may consume free credits")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to perform real network discovery")
    directory = ROOT / "data" / "verify_product_live" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        code = asyncio.run(run(args, report, directory))
    except Exception as exc:
        # Exception messages can contain provider request details; record only type.
        report.update(status="failed_exception", error_type=type(exc).__name__)
        code = 1
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(directory / "report.json")}, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
