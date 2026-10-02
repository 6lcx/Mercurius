import json
from types import SimpleNamespace

import pytest

from scripts.benchmark.aggregate import aggregate, percentile, stats, load_scored_rows
from scripts.benchmark.cases import evaluate, contains_amount
from scripts.benchmark.observation import usage_fields, ObservedStream
from scripts.benchmark.common import digest


@pytest.mark.asyncio
async def test_compression_fixtures_use_actual_service_facts():
    from scripts.benchmark.live import compression_fixtures
    cases, fixtures = await compression_fixtures()
    assert len(cases) == 6
    assert sorted(c['turns'] for c in cases) == [4, 4, 8, 8, 16, 16]
    for case in cases:
        hit = fixtures[case['id']]['hits'][0]
        assert hit['product_id'] == case['returned_product_id']
        assert hit['title'] and hit['price_major'] > 0
    # A real search may rank another product first. It is that result's facts
    # that must be retained, not the fixture query's originating product.
    assert any(c['query_source_product_id'] != c['returned_product_id'] for c in cases)


def test_missing_cache_is_not_zero():
    assert usage_fields({'prompt_tokens': 10})['cached_tokens'] is None
    assert usage_fields({'prompt_tokens': 10, 'prompt_cache_hit_tokens': 0})['cached_tokens'] == 0
    assert usage_fields({'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 8}})['cached_tokens'] == 8


def test_empty_transcript_never_passes():
    for name in ('search-budget', 'order-full-cycle', 'memory-write', 'chitchat-boundary'):
        assert not all(evaluate({'id': name, 'queries': ['x']}, []).values())


def test_money_equivalence_and_numeric_boundaries():
    assert contains_amount('到手价 ¥154；另一个 ¥1,619.90。', 154.0)
    assert contains_amount('到手价 ¥154；另一个 ¥1,619.90。', 1619.9)
    assert not contains_amount('¥1154、¥154.01、P0154', 154)
    assert contains_amount('CNY 154.00', 154)
    assert contains_amount('总价154元或CNY154.00', 154)
    assert not contains_amount('USD12,154', 154)


def test_score_review_preserves_raw_and_rejects_changed_evidence(tmp_path):
    row = dict(suite='agent', case_id='amount', variant='on', repeat=0,
               success=False, checks={'quoted_total': False}, elapsed_s=1)
    raw = tmp_path / 'results.jsonl'
    raw.write_text(json.dumps(row)+'\n', encoding='utf-8')
    original = raw.read_bytes()
    review = tmp_path / 'score-review.jsonl'
    review.write_text(json.dumps(dict(row, success=True, checks={'quoted_total': True}))+'\n', encoding='utf-8')
    (tmp_path / 'score-review-manifest.json').write_text(json.dumps(dict(
        raw_results_sha256=digest(raw), review_sha256=digest(review))), encoding='utf-8')
    scored = load_scored_rows(tmp_path)[0]
    assert scored['success'] and not scored['original_success']
    assert raw.read_bytes() == original
    raw.write_bytes(original+b'\n')
    with pytest.raises(ValueError, match='stale'):
        load_scored_rows(tmp_path)


def test_aggregate_keeps_failures_and_missing_usage(tmp_path):
    rows = [dict(suite='s', case_id='a', variant='v', repeat=0, success=True, elapsed_s=1, input_tokens=10, cached_tokens=0),
            dict(suite='s', case_id='b', variant='v', repeat=0, success=False, elapsed_s=9, input_tokens=50, cached_tokens=None)]
    (tmp_path / 'results.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
    group = aggregate(tmp_path)['groups'][0]
    assert group['success_rate'] == .5
    assert group['metrics']['elapsed_s']['mean'] == 5
    assert group['elapsed_success_only']['mean'] == 1
    assert group['cache']['eligible_requests'] == 1
    assert group['cache']['token_hit_rate'] == 0
    assert percentile([1, 9], .9) == pytest.approx(8.2)
    assert stats([])['mean'] is None


@pytest.mark.asyncio
async def test_stream_observer_preserves_context_usage_and_close():
    class Source:
        closed = False
        def __aiter__(self):
            async def items():
                yield SimpleNamespace(usage=None, model='m')
                yield SimpleNamespace(usage={'prompt_tokens': 100, 'prompt_cache_hit_tokens': 80}, model='m')
            return items()
        async def close(self):
            self.closed = True
    source, row, finished = Source(), {}, []
    async with ObservedStream(source, row, lambda error=None: finished.append(error)) as stream:
        assert len([x async for x in stream]) == 2
    assert row['cached_tokens'] == 80
    assert row['input_tokens'] == 100
    assert source.closed
    assert all(e is None for e in finished)


@pytest.mark.asyncio
async def test_stream_observer_does_not_hide_midstream_failure():
    class Source:
        def __aiter__(self):
            async def items():
                yield SimpleNamespace(usage=None)
                raise RuntimeError('broken')
            return items()
        async def close(self):
            pass
    finished = []
    with pytest.raises(RuntimeError, match='broken'):
        async with ObservedStream(Source(), {}, lambda error=None: finished.append(error)) as stream:
            async for _ in stream:
                pass
    assert any(isinstance(e, RuntimeError) for e in finished)
