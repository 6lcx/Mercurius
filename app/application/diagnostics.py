"""Request-level fault triage from business events, without another model call."""
from __future__ import annotations


def diagnose_request(events, request_id):
    """Return evidence and first causal candidates; never claim a proven root cause.

    Earlier tool/upstream failures outrank downstream circuit-open messages.
    Input is the already-recorded business event stream, not arbitrary log text.
    """
    selected = [e for e in events if e.get('request_id') == request_id]
    selected.sort(key=lambda e: e.get('occurred_at', ''))
    failures, consequences, timeline, trace_ids = [], [], [], set()
    for index, event in enumerate(selected):
        payload = event.get('payload')
        payload = payload if isinstance(payload, dict) else {}
        kind = event.get('type', '')
        row = {'index': index, 'at': event.get('occurred_at'), 'event': kind,
               'tool': payload.get('tool'), 'traceparent': event.get('traceparent', '')}
        traceparent = row['traceparent'].split('-')
        if len(traceparent) == 4:
            trace_ids.add(traceparent[1])
        timeline.append(row)
        if kind in {'model.attempt_failed', 'model.stream_failed'}:
            failures.append({**row, 'classification': payload.get('cause_code') or payload.get('code', 'unclassified_error'),
                             'attempt': payload.get('attempt'), 'model': payload.get('model'),
                             'retryable': payload.get('retryable', False),
                             'partial_output': payload.get('partial_output', False)})
            continue
        message = payload.get('error') or payload.get('reason')
        if kind == 'error':
            message = message or payload.get('message')
        if not message:
            continue
        finding = {**row, 'message': str(message)}
        text = str(message).lower()
        if payload.get('circuit') == 'open' and any(s in text for s in ('暂不可用', '短路', 'circuit open')):
            finding['classification'] = 'circuit_rejection'
            consequences.append(finding)
        elif kind == 'model.fallback':
            finding['classification'] = 'fallback_activated'
            consequences.append(finding)
        else:
            finding['classification'] = (
                'rate_limit' if any(s in text for s in ('429', 'rate limit', 'too many requests')) else
                'timeout' if any(s in text for s in ('timeout', 'timed out', '超时', '执行超过')) else
                'connection_failure' if any(s in text for s in ('connection reset', 'connection refused')) else
                'upstream_unavailable' if any(s in text for s in ('503', 'service unavailable')) else
                'unclassified_error')
            failures.append(finding)
    return {'request_id': request_id, 'matched_events': len(selected), 'trace_ids': sorted(trace_ids),
            'first_failure': failures[0] if failures else None, 'failure_candidates': failures,
            'downstream_consequences': consequences, 'timeline': timeline,
            'status': 'failure_evidence_found' if failures else 'consequences_only' if consequences else 'no_failure_evidence',
            'scope': 'Evidence-assisted triage. An error classification is not proof of the underlying root cause.'}
