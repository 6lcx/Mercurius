"""Worker output must agree with that worker's successful search evidence."""
import json
import re


def search_contract(text, events):
    searches = [e['payload'] for e in events if e.get('type') == 'tool.result'
                and e.get('payload', {}).get('tool') == 'product_search_tool'
                and not e['payload'].get('error') and isinstance(e['payload'].get('hits'), list)]
    if not searches:
        return False
    try:
        payload = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip()))
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get('hits'), list):
        return False
    ids = {h['product_id'] for s in searches for h in s['hits'] if isinstance(h, dict) and h.get('product_id')}
    if not payload['hits']:
        return not ids  # Truthful no-stock/no-match response is a valid outcome.
    return all(isinstance(h, dict) and h.get('product_id') in ids for h in payload['hits'])
