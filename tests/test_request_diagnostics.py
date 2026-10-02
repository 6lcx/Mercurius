from app.application.diagnostics import diagnose_request


def event(request, at, payload, kind='tool.result'):
    return {'request_id': request, 'occurred_at': at, 'payload': payload, 'type': kind}


def test_first_upstream_failure_outranks_later_circuit_consequence_and_other_requests():
    rows = [event('other', '01', {'error': 'invalid order'}),
            event('mine', '03', {'tool': 'search', 'circuit': 'open', 'error': '暂不可用'}),
            event('mine', '02', {'tool': 'search', 'error': '503 service unavailable'})]
    result = diagnose_request(rows, 'mine')
    assert result['matched_events'] == 2
    assert result['first_failure']['classification'] == 'upstream_unavailable'
    assert result['downstream_consequences'][0]['classification'] == 'circuit_rejection'
    assert 'invalid order' not in str(result)


def test_missing_failure_evidence_is_not_invented():
    assert diagnose_request([], 'missing')['status'] == 'no_failure_evidence'
    result = diagnose_request([event('x', '01', {'reason': '503', 'to': 'backup'}, 'model.fallback')], 'x')
    assert result['first_failure'] is None
    assert result['status'] == 'consequences_only'
