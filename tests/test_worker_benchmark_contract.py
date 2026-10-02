from scripts.benchmark.worker_contract import search_contract


def result(hits, error=None):
    return [{'type':'tool.result', 'payload':dict(tool='product_search_tool', hits=hits, error=error)}]


def test_truthful_empty_result_requires_successful_empty_search():
    assert search_contract('{"hits":[]}', result([]))
    assert not search_contract('{"hits":[]}', [])
    assert not search_contract('{"hits":[]}', result([], 'timeout'))
    assert not search_contract('{"hits":[]}', result([{'product_id':'P1'}]))


def test_worker_cannot_claim_unevidenced_products_or_omit_result_structure():
    evidence = result([{'product_id':'P1'}])
    assert search_contract('```json\n{"hits":[{"product_id":"P1"}]}\n```', evidence)
    assert not search_contract('{"hits":[{"product_id":"P2"}]}', evidence)
    assert not search_contract('{}', result([]))
