"""Product-only recommendation, conditional discovery and stable recommendation snapshots.

Scores are transparent demo heuristics, not factual quality certificates. Knowledge
articles never participate. Unknown shipping/tax is never treated as free.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from app.application.usecases.catalog_search import CatalogSearchUseCase, tokenize
from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.catalog.money import Money
from app.domain.shipping.tariff_schedule import TariffSchedule
from app.infrastructure.context import ShoppingContext

_CONCEPTS = {
    'camping': ('露营', 'camping', 'camp'),
    'lantern': ('露营灯', '营地灯', 'lantern', 'camping light'),
    'rain': ('雨天', '防水', '防雨', 'waterproof', 'water resistant', 'ipx4', 'ipx5', 'ipx6', 'ipx7', 'ip67', 'ip68', 'ip66'),
    'charging': ('充电', 'rechargeable', 'usb-c', 'usb c', 'type-c'),
    'battery': ('续航', 'runtime', 'battery life', 'long lasting'),
    'portable': ('便携', '轻便', 'portable', 'lightweight'),
    'backpack': ('背包', 'backpack'),
    'headphones': ('耳机', 'headphone', 'earbud'),
    'powerbank': ('充电宝', '移动电源', 'power bank', 'powerbank'),
    'suitcase': ('行李箱', '拉杆箱', 'suitcase', 'luggage'),
    'charger': ('充电器', 'charger'),
    'tent': ('帐篷', 'tent'),
}


_PRODUCT_KINDS = ('lantern', 'backpack', 'headphones', 'powerbank', 'suitcase', 'charger', 'tent')

def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


class ProductRecommendationService:
    def __init__(self, repo, discovery=None, tariff_schedule=None, embedder=None,
                 cache_path=None, quality_threshold=.55, weight=.8, ttl_seconds=86400, preference_store=None):
        if not 0 < weight < 1 or not 0 < quality_threshold <= 1 or ttl_seconds <= 0:
            raise ValueError('invalid recommendation policy')
        self._preference_store = preference_store
        self._repo = repo
        self._discovery = discovery
        self._tariff = tariff_schedule or TariffSchedule(rates=ExchangeRateTable())
        self._cards = CatalogSearchUseCase(repo, tariff_schedule=self._tariff)
        self._embedder = embedder
        self._threshold = quality_threshold
        self._weight = weight
        self._ttl = ttl_seconds
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache = {}
        self._request_locks = {}
        if self._cache_path and self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                self._cache = {}

    async def execute(self, spec, *, session_key=None, refresh=False):
        snapshot = ShoppingContext.current()
        current = (snapshot.session_data or {}).get('current_shopping', {}) if snapshot else {}
        # Persisted current intent overrides old parameters reconstructed from a
        # summary. New independent shopping tasks explicitly reset this state.
        constraints = {k: current[k] for k in ('price_max_major', 'target_currency', 'ship_to') if k in current}
        if constraints:
            spec = replace(spec, **constraints)
        if self._preference_store is not None and snapshot is not None:
            try:
                preferences = await self._preference_store.list_by_buyer(snapshot.buyer_id)
            except Exception:
                return self._constraint_failure('preference_store_unavailable', [])
            brands, materials, colors, unresolved = list(spec.excluded_brands), list(spec.excluded_materials), list(spec.excluded_colors), []
            exceptions = snapshot.turn_data.get('preference_exceptions', {})
            for preference in preferences:
                if preference.kind != 'dislike':
                    continue
                if preference.statement in exceptions:
                    continue
                statement = preference.statement.strip()
                # Only parse explicit supported forms; never guess arbitrary
                # natural-language constraints and present them as verified.
                material = re.fullmatch(r'(?:\u4e0d\u8981|\u6392\u9664|\u907f\u5f00)(.+?)\u6750\u8d28', statement)
                brand = re.fullmatch(r'(?:\u4e0d\u8981|\u6392\u9664|\u907f\u5f00)(.+?)\u54c1\u724c', statement)
                color = re.fullmatch(r'(?:不喜欢|不要|排除|避开)(红色|蓝色|黑色|白色|绿色|黄色)(?:的?商品)?', statement)
                if material:
                    materials.append(material.group(1))
                elif brand:
                    brands.append(brand.group(1))
                elif color:
                    colors.append(color.group(1))
                elif statement.lower().startswith('material:'):
                    materials.append(statement.split(':', 1)[1].strip())
                elif statement.lower().startswith('brand:'):
                    brands.append(statement.split(':', 1)[1].strip())
                elif statement.lower().startswith('color:'):
                    colors.append(statement.split(':', 1)[1].strip())
                else:
                    unresolved.append(statement)
            if unresolved:
                return self._constraint_failure('constraints_require_clarification', unresolved)
            spec = replace(spec, excluded_brands=tuple(sorted(set(brands))),
                           excluded_materials=tuple(sorted(set(materials))),
                           excluded_colors=tuple(sorted(set(colors))))
        owner = session_key or (snapshot.buyer_id if snapshot else 'anonymous')
        key = json.dumps([owner, asdict(spec)], sort_keys=True, ensure_ascii=False)
        lock, users = self._request_locks.get(key, (asyncio.Lock(), 0))
        self._request_locks[key] = (lock, users + 1)
        try:
            async with lock:
                return await self._execute(spec, session_key, refresh)
        finally:
            _, users = self._request_locks[key]
            if users == 1:
                del self._request_locks[key]
            else:
                self._request_locks[key] = (lock, users - 1)

    @staticmethod
    def _constraint_failure(reason, unresolved):
        return {'hits': [], 'total_candidates': 0, 'filtered_out': [],
                'unverified_candidates': [], 'discovery_status': reason,
                'recall_strategy': 'constraint_validation', 'gate': {},
                'admitted_ids': [], 'updated_ids': [],
                'unresolved_constraints': unresolved,
                'constraint_notice': 'Clarify these exclusions before recommending; no product has been verified.'}

    async def _execute(self, spec, session_key, refresh):
        if spec.price_max_major is not None and _number(spec.price_max_major) is None:
            raise ValueError('price_max_major must be finite and nonnegative')
        snapshot = ShoppingContext.current()
        owner = session_key or (snapshot.buyer_id if snapshot else 'anonymous')
        key = hashlib.sha256(json.dumps([owner, asdict(spec)], sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()
        products = await self._repo.list_all()
        scored, rejected, unverified = await self._evaluate(products, spec)
        by_id = {row['product_id']: row for row in scored}
        good = [row for row in scored if row['score'] >= self._threshold]
        gate = {'quality_threshold': self._threshold, 'external_weight': self._weight,
                'local_tail': good[min(spec.top_k, len(good)) - 1]['score'] if good else None,
                'local_good_count': len(good)}
        cached = self._cache.get(key)
        if not refresh and cached and time.time() - cached['created_at'] < self._ttl:
            ids = cached['ids']
            # Never replay stale prices or a newly invalid stock/destination/budget.
            if ids and all(pid in by_id and by_id[pid]['score'] >= self._threshold for pid in ids):
                return self._result([by_id[pid] for pid in ids], 'cache_hit', gate, [], rejected, unverified)
        if len(good) >= spec.top_k:
            hits = good[:spec.top_k]
            self._remember(key, hits)
            return self._result(hits, 'local_sufficient', gate, [], rejected, unverified)
        status = 'unavailable' if self._discovery is None else 'discovered'
        admitted = []
        updated = []
        if self._discovery is not None:
            try:
                candidates = await self._discovery.discover(spec)
            except Exception:
                candidates = []
                status = 'discovery_failed'
            # Dedup URL against the retained local catalog, not just one result batch.
            known_urls = {p.purchase_url.rstrip('/') for p in products if p.purchase_url}
            existing_by_url = {p.purchase_url.rstrip('/'): p for p in products if p.purchase_url}
            known_ids = {p.product_id for p in products}
            normalized = {}
            update_ids = set()
            for candidate in candidates:
                if not candidate.purchase_url:
                    continue
                existing = existing_by_url.get(candidate.purchase_url.rstrip('/'))
                if existing is not None:
                    if existing.source_type == 'local':
                        continue  # Never overwrite the seed catalog.
                    candidate = replace(candidate, product_id=existing.product_id)
                    update_ids.add(existing.product_id)
                elif candidate.product_id in known_ids:
                    continue
                normalized[candidate.product_id] = candidate
            candidates = list(normalized.values())
            new_scores, new_rejected, new_unverified = await self._evaluate(candidates, spec)
            rejected.extend(new_rejected)
            unverified.extend(new_unverified)
            candidate_map = {p.product_id: p for p in candidates}
            retained = [row for row in good if row['product_id'] not in update_ids]
            retained_ids = {row['product_id'] for row in retained}
            winners = {row['product_id'] for row in sorted(
                retained + new_scores,
                key=lambda r: (-r['score'], r['product_id'] not in retained_ids, r['product_id'])
            )[:spec.top_k]}
            for card in new_scores:
                # Shortage route uses the same minimum threshold, no impossible tail.
                if card['score'] < self._threshold or card['product_id'] not in winners:
                    continue
                product = candidate_map[card['product_id']]
                url = product.purchase_url.rstrip('/')
                if url in known_urls and product.product_id not in update_ids:
                    continue
                try:
                    await self._repo.upsert(product)
                except Exception:
                    status = 'storage_failed'
                    continue
                known_urls.add(url)
                if product.product_id in update_ids:
                    updated.append(product.product_id)
                    good = [row for row in good if row['product_id'] != product.product_id]
                else:
                    admitted.append(product.product_id)
                good.append(card)
        good.sort(key=lambda row: (-row['score'], row['product_id']))
        hits = good[:spec.top_k]
        if status == 'discovered':
            self._remember(key, hits)
        return self._result(hits, status, gate, admitted, rejected, unverified, updated)

    def _remember(self, key, hits):
        now = time.time()
        self._cache = {k: v for k, v in self._cache.items() if now - v['created_at'] < self._ttl}
        # Empty results must not suppress future availability or network recovery.
        if hits:
            self._cache[key] = {'created_at': now, 'ids': [h['product_id'] for h in hits]}
        if self._cache_path:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self._cache, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self._cache_path)

    @staticmethod
    def _result(hits, status, gate, admitted, rejected, unverified, updated=None):
        return {'hits': hits, 'total_candidates': len(hits),
                'recall_strategy': 'product_catalog_rules', 'rerank_applied': False,
                'discovery_status': status, 'gate': gate, 'admitted_ids': admitted, 'updated_ids': updated or [],
                'filtered_out': rejected[:10], 'unverified_candidates': unverified[:5],
                'scoring_policy': 'demand .70, affordability .10, source .08, warranty .05, sales .04, brand .03; external discount',
                'cost_notice': 'Currency conversions and local tariff quotes use demo estimates; unknown external fees are not zero.'}

    async def _evaluate(self, products, spec):
        rows, rejected, unverified = [], [], []
        semantic = {}
        if self._embedder is not None and products:
            try:
                query_vector = await self._embedder.embed(spec.normalized_query)
                vectors = await asyncio.gather(*(self._embedder.embed(p.searchable_text()) for p in products))
                for product, vector in zip(products, vectors):
                    if len(vector) != len(query_vector):
                        continue
                    norm = math.sqrt(sum(x*x for x in vector) * sum(x*x for x in query_vector))
                    similarity = sum(a*b for a, b in zip(vector, query_vector)) / norm if norm else 0
                    if math.isfinite(similarity):
                        semantic[product.product_id] = max(0., min(1., similarity))
            except Exception:
                pass  # Deterministic lexical recall remains available offline.
        for product in products:
            external = product.source_type != 'local'
            evidence = product.evidence or {}
            exclusion = self._check_exclusions(product, spec)
            if exclusion:
                target = unverified if exclusion.endswith('_unknown') else rejected
                target.append({'product_id': product.product_id, 'reason': exclusion})
                continue
            if external and self._stale(evidence):
                rejected.append({'product_id': product.product_id, 'reason': 'external_evidence_stale'})
                continue
            if (not external and product.primary_sku().stock <= 0) or evidence.get('availability') == 'out_of_stock':
                rejected.append({'product_id': product.product_id, 'reason': 'out_of_stock'})
                continue
            if spec.ship_to and product.ships_to and spec.ship_to not in product.ships_to:
                rejected.append({'product_id': product.product_id, 'reason': 'ship_to_unavailable'})
                continue
            if not self._kind_matches(product, spec):
                rejected.append({'product_id': product.product_id, 'reason': 'product_kind_mismatch'})
                continue
            fit = max(self._fit(product, spec), semantic.get(product.product_id, 0.))
            if fit < .2:
                rejected.append({'product_id': product.product_id, 'reason': 'demand_mismatch'})
                continue
            landed, amount = self._landed(product, spec)
            base = self._tariff.rates.convert(product.primary_sku().price, spec.target_currency).to_major_units()
            if spec.price_max_major is not None and (amount if amount is not None else base) > spec.price_max_major:
                rejected.append({'product_id': product.product_id, 'reason': 'over_price_cap'})
                continue
            dimensions = self._dimensions(product, fit, amount, spec)
            raw = sum(dimensions[k] * w for k, w in (
                ('demand', .70), ('affordability', .10), ('source', .08),
                ('warranty', .05), ('sales', .04), ('brand', .03)))
            score = raw * (self._weight if external else 1)
            card = self._cards._to_card(score, product, spec).to_dict()
            card.update(purchase_url=product.purchase_url, source_type=product.source_type,
                        evidence={k: (v[:400] if isinstance(v, str) else v) for k, v in evidence.items()
                                  if k not in {'raw_evidence', 'description', 'content'}},
                        requirement_notice='Demand fit is retrieval evidence, not verification of every numeric specification.',
                        landed_price=landed, score=round(score, 6),
                        raw_score=round(raw, 6), score_dimensions=dimensions,
                        score_reasons={'source': evidence.get('source_verification', 'unknown; neutral score'),
                                       'warranty': str(evidence.get('warranty_evidence', 'unknown; neutral score'))[:200],
                                       'sales': str(evidence.get('sales_evidence', 'unknown; neutral score'))[:200],
                                       'brand': str(evidence.get('brand_evidence', 'unknown; neutral score'))[:200]},
                        budget_status=('unverified' if amount is None else
                                       'within_estimated_budget' if spec.price_max_major is not None else 'not_requested'))
            if external and evidence.get('availability', 'unknown') == 'unknown':
                for sku in card['skus']:
                    sku['stock'] = None
            if spec.price_max_major is not None and amount is None:
                card['reason'] = 'landed_cost_unknown'
                unverified.append(card)
            else:
                rows.append(card)
        rows.sort(key=lambda row: (-row['score'], row['product_id']))
        return rows, rejected, unverified

    @staticmethod
    def _check_exclusions(product, spec):
        aliases = {'\u5851\u6599': 'plastic', '\u5851\u80f6': 'plastic',
                   '红色': 'red', '蓝色': 'blue', '黑色': 'black', '白色': 'white', '绿色': 'green', '黄色': 'yellow',
                   '\u94dd': 'aluminum', 'aluminium': 'aluminum',
                   '\u4e0d\u9508\u94a2': 'stainless steel'}
        def canonical(value):
            value = str(value).strip().casefold()
            return aliases.get(value, value)
        brands = {canonical(b) for b in spec.excluded_brands}
        if brands:
            if not product.brand.strip():
                return 'brand_exclusion_unknown'
            if canonical(product.brand) in brands:
                return 'excluded_brand'
        materials = {canonical(m) for m in spec.excluded_materials}
        if materials:
            known = (product.evidence or {}).get('materials')
            if not isinstance(known, list) or not known:
                return 'material_exclusion_unknown'
            if materials.intersection(canonical(m) for m in known):
                return 'excluded_material'
        colors = {canonical(c) for c in spec.excluded_colors}
        if colors:
            known = (product.evidence or {}).get('colors')
            if not isinstance(known, list) or not known:
                return 'color_exclusion_unknown'
            if colors.intersection(canonical(c) for c in known):
                return 'excluded_color'
        return None

    @staticmethod
    def _stale(evidence):
        value = evidence.get('fetched_at')
        if not value:
            return False
        try:
            fetched = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - fetched).total_seconds() > 7 * 86400
        except (TypeError, ValueError):
            return True  # Malformed freshness evidence cannot establish freshness.

    @staticmethod
    def _kind_matches(product, spec):
        query = spec.normalized_query.lower()
        wanted = [kind for kind in _PRODUCT_KINDS
                  if any(alias in query for alias in _CONCEPTS[kind])]
        if not wanted:
            return True
        # Descriptions often list compatible devices; identity must come from title.
        title = product.title.lower()
        return any(any(alias in title for alias in _CONCEPTS[kind]) for kind in wanted)

    @staticmethod
    def _fit(product, spec):
        query, document = spec.normalized_query.lower(), product.searchable_text().lower()
        terms = tokenize(query)
        lexical = min(1., len(terms & tokenize(document)) / max(1, min(len(terms), 8)))
        wanted = [key for key, aliases in _CONCEPTS.items() if any(a in query for a in aliases)]
        concept = sum(any(a in document for a in _CONCEPTS[key]) for key in wanted) / len(wanted) if wanted else 0
        fit = max(lexical, concept)
        if spec.category and spec.category.lower() not in product.category.lower():
            fit *= .7
        return fit

    def _dimensions(self, product, fit, amount, spec):
        e = product.evidence or {}
        source = {'official': .9, 'authorized': .85, 'marketplace': .65}.get(e.get('source_verification'), .5)
        if e.get('source_confidence') == 'verified':
            source = .8
        warranty = .5
        months = _number(e.get('warranty_months'))
        if months is not None:
            warranty = .5 + .4 * min(months / 24, 1)
        elif e.get('warranty_evidence'):
            warranty = .6  # Explicit seller claim, not verified entitlement.
        sales = .5
        count = _number(e.get('sales_count'))
        if count is not None:
            sales = .5 + .2 * min(math.log1p(count) / math.log1p(10000), 1)
        elif e.get('sales_evidence'):
            sales = .55
        brand = .8 if e.get('brand_verified') else .55 if e.get('brand_evidence') else .5
        affordability = .5
        if amount is not None:
            # Common currency reference keeps price useful even without a budget.
            cny = self._tariff.rates.convert(Money.from_major_units(amount, spec.target_currency), 'CNY').to_major_units()
            affordability = 1 / (1 + cny / 300)
        if amount is not None and spec.price_max_major and spec.price_max_major > 0:
            affordability = .5 + .5 * max(0, 1 - amount / spec.price_max_major)
        return dict(demand=round(fit, 4), affordability=round(affordability, 4),
                    source=source, warranty=round(warranty, 4), sales=round(sales, 4), brand=brand)

    def _landed(self, product, spec):
        if not spec.ship_to:
            return {'status': 'unknown', 'unavailable_reason': 'destination_required'}, None
        if product.source_type == 'local':
            try:
                quote = self._tariff.quote(product.primary_sku().price, product.category,
                                           spec.ship_to, 1, spec.target_currency).to_dict()
                return dict(quote, status='estimated'), quote['landed_total_major']
            except ValueError:
                return {'status': 'unknown', 'unavailable_reason': 'destination_unsupported'}, None
        e = product.evidence or {}
        freight, tax = _number(e.get('shipping_major')), _number(e.get('tax_major'))
        if freight is None or tax is None or e.get('ship_to') != spec.ship_to:
            return {'status': 'unknown', 'unavailable_reason': 'shipping_tax_or_delivery_unverified'}, None
        currency = e.get('cost_currency', product.primary_sku().price.currency)
        try:
            subtotal = self._tariff.rates.convert(product.primary_sku().price, spec.target_currency).to_major_units()
            freight = self._tariff.rates.convert(Money.from_major_units(freight, currency), spec.target_currency).to_major_units()
            tax = self._tariff.rates.convert(Money.from_major_units(tax, currency), spec.target_currency).to_major_units()
        except ValueError:
            return {'status': 'unknown', 'unavailable_reason': 'currency_unsupported'}, None
        total = round(subtotal + freight + tax, 2)
        return {'status': 'estimate', 'basis': 'source_costs_with_demo_exchange_rates',
                'subtotal_major': subtotal, 'freight_major': freight, 'tariff_major': tax,
                'landed_total_major': total, 'currency': spec.target_currency, 'ship_to': spec.ship_to}, total
