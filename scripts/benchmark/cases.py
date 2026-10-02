"""Frozen contract predicates. Do not edit expectations in response to scores."""
import re
from decimal import Decimal, InvalidOperation
import yaml
from .common import ROOT


def contains_amount(text, amount):
    """Compare exact numeric values; 154 and 154.0 express the same amount."""
    expected = Decimal(str(amount))
    normalized = re.sub(r'\b(?:CNY|USD|RMB)', '', text, flags=re.IGNORECASE)
    for token in re.findall(r'(?<![A-Za-z0-9_.,])\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z0-9_.])', normalized):
        try:
            if Decimal(token.replace(',', '')) == expected:
                return True
        except InvalidOperation:
            continue
    return False


def agent_cases():
    cases = yaml.safe_load((ROOT / 'eval/cases.yaml').read_text(encoding='utf-8'))['cases']
    # Explicit current-contract changes, before any live response is observed.
    for case in cases:
        if case['id'] == 'memory-recall':
            case['queries'] = ['帮我推荐旅行三件套；如果材质没有核实，请明确告诉我不能确认，不要猜。']
            case['preference_fixture'] = '不要塑料材质'
        if case['id'] == 'search-budget':
            # Current service requires a destination to verify a landed budget.
            case['queries'] = ['帮我找一款寄到中国、到手价300元以内、抗造耐摔的露营灯。']
        if case['id'] == 'long-context-memory':
            case['queries'][0] = '帮我找一款寄到中国、300元以内、抗造耐摔的露营灯。'
    return cases


def evaluate(case, turns):
    """Return independent assertions with visible evidence, not LLM self-reports.

    These are minimum machine-verifiable task contracts, not a complete human
    assessment of shopping advice. Final text and full state remain available.
    """
    assertions = {}
    name = case['id']
    texts = [r['text'] for r in turns]
    text = '\n'.join(texts)
    events = [e for t in turns for e in t['events']]
    calls = [e['payload'] for e in events if e['type'] == 'tool.invoke']
    results = [e['payload'] for e in events if e['type'] == 'tool.result']
    hits = [h for r in results if r.get('tool') == 'product_search_tool' for h in r.get('hits', [])]
    assertions['all_turns_returned'] = len(turns) == len(case['queries']) and all(t['transport_success'] and bool(t['text'].strip()) for t in turns)
    assertions['no_unresolved_permission'] = not any(t.get('permission_pending') for t in turns)
    assertions['no_unknown_product_ids'] = all(pid in turns[-1]['catalog_ids'] for pid in re.findall(r'\bP\d{4}\b', text)) if turns else False
    if name == 'search-budget':
        assertions['budget_passed_to_tool'] = any(c.get('tool') == 'product_search_tool' and c.get('args', {}).get('price_max_major') is not None and float(c['args']['price_max_major']) <= 300 and c['args'].get('ship_to') == 'CN' for c in calls)
        assertions['relevant_hit'] = any(h['product_id'] == 'P1008' for h in hits)
        assertions['hits_within_budget'] = bool(hits) and all(h.get('landed_price', {}).get('landed_total_major', float('inf')) <= 300 for h in hits)
        assertions['final_contains_target'] = 'LumenGo' in text or 'P1008' in text
    elif name in ('landed-price-us', 'compare-two'):
        expected = ['AeroHush'] if name == 'landed-price-us' else ['AeroHush', 'VoltTrek']
        for brand in expected:
            matching = [h for h in hits if brand in h['title']]
            assertions[f'{brand}_tool_evidence'] = bool(matching)
            assertions[f'{brand}_final'] = brand in text
            valid = []
            for hit in matching:
                quote = hit.get('landed_price', {})
                total = quote.get('landed_total_major')
                valid.append(total is not None and contains_amount(text, total))
            assertions[f'{brand}_quoted_total'] = any(valid)
    elif name in ('order-confirm-card', 'order-full-cycle'):
        assertions['no_order_before_confirmation'] = bool(turns) and not turns[0]['orders']
        assertions['stock_unchanged_before_confirmation'] = bool(turns) and turns[0]['lamp_stock'] == turns[0]['initial_lamp_stock']
        assertions['confirmation_requested'] = bool(texts) and '确认' in texts[0]
        if name == 'order-full-cycle':
            ordered = turns[1]['orders'] if len(turns) > 1 else []
            assertions['one_confirmed_order'] = len(ordered) == 1 and ordered[0]['status'] == 'CONFIRMED'
            assertions['order_amount'] = len(ordered) == 1 and ordered[0]['total_amount_minor'] == 8900
            assertions['exact_order_line'] = len(turns) > 1 and len(turns[1]['order_lines']) == 1 and all(l['product_id'] == 'P1008' and l['sku_id'] == 'P1008-S1' and l['quantity'] == 1 for l in turns[1]['order_lines'])
            assertions['single_stock_decrement'] = len(turns) > 1 and turns[1]['lamp_stock'] == turns[1]['initial_lamp_stock']-1
            assertions['cancelled_and_restored'] = len(turns) == 3 and len(turns[-1]['orders']) == 1 and turns[-1]['orders'][0]['status'] == 'CANCELLED' and turns[-1]['lamp_stock'] == turns[-1]['initial_lamp_stock']
    elif name == 'memory-write':
        assertions['write_invoked'] = any(c.get('tool') == 'remember_preference_tool' for c in calls)
        assertions['preference_persisted'] = bool(turns) and any(p['kind'] == 'dislike' and '塑料' in p['statement'] for p in turns[-1]['preferences'])
    elif name == 'memory-recall':
        assertions['fixture_persisted'] = bool(turns) and any(p['statement'] == case['preference_fixture'] for p in turns[-1]['preferences'])
        assertions['preference_acknowledged'] = '塑料' in text
        assertions['no_unverified_material_hits'] = all(h.get('evidence', {}).get('materials') and not any(x.lower() in ('plastic', '塑料') for x in h['evidence']['materials']) for h in hits)
        assertions['search_or_clear_clarification'] = any(c.get('tool') == 'product_search_tool' for c in calls) or any(w in text for w in ('核实', '确认', '材质信息'))
    elif name == 'no-fabrication':
        assertions['search_invoked'] = any(c.get('tool') == 'product_search_tool' for c in calls)
        assertions['no_invented_drone_offer'] = not re.search(r'\bP\d{4}\b', text) and any(w in text for w in ('没有', '未找到', '暂无', '没找到', '不支持'))
    elif name == 'order-not-found':
        assertions['lookup_failed_with_evidence'] = any(r.get('tool') == 'query_order_tool' and r.get('error') for r in results)
        assertions['honest_final'] = any(w in text for w in ('不存在', '未找到', '没有找到', '查不到'))
    elif name == 'chitchat-boundary':
        assertions['no_business_tool_calls'] = not calls
        assertions['shopping_role'] = any(w in text for w in ('购物', 'Mercurius', '选品', '商品'))
    elif name == 'category-insight':
        assertions['knowledge_retrieved'] = any(r.get('tool') == 'category_insight_tool' and r.get('hit_count', 0) > 0 for r in results)
        assertions['no_product_search'] = not any(c.get('tool') == 'product_search_tool' for c in calls)
        assertions['requested_topics'] = '材质' in text and any(w in text for w in ('重量', '自重', '轻'))
    elif name == 'long-context-memory':
        assertions['recall_final_facts'] = bool(texts) and ('LumenGo' in texts[-1] or 'P1008' in texts[-1]) and '89' in texts[-1]
        assertions['original_fact_from_tool'] = any(h['product_id'] == 'P1008' and h['price_major'] == 89 for h in hits)
    elif name == 'tool-error-honesty':
        assertions['search_invoked'] = any(c.get('tool') == 'product_search_tool' for c in calls)
        assertions['unknown_cost_explained'] = any(w in text for w in ('不支持', '无法', '不能', '暂不', '未能'))
        assertions['no_total_claim'] = not re.search(r'(?:到手价|到手总价|运费|关税)\s*[:：为约是]?\s*[¥￥$]?\s*\d', text)
    else:
        raise ValueError('No frozen evaluator for ' + name)
    return assertions
