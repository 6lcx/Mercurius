from __future__ import annotations
import hashlib
import json
import time
from unittest.mock import patch
from .common import TRIAL, now, safe_error


def usage_fields(usage):
    raw = usage.model_dump() if hasattr(usage, 'model_dump') else usage
    if not isinstance(raw, dict):
        return dict(raw_usage=None, input_tokens=None, output_tokens=None, cached_tokens=None)
    cached = raw.get('prompt_cache_hit_tokens')
    if cached is None and isinstance(raw.get('prompt_tokens_details'), dict):
        cached = raw['prompt_tokens_details'].get('cached_tokens')
    return dict(raw_usage=raw, input_tokens=raw.get('prompt_tokens'),
                output_tokens=raw.get('completion_tokens'), cached_tokens=cached)


class ApiCapture:
    def __init__(self, rec, max_calls):
        self.rec, self.max_calls, self.calls = rec, max_calls, []
        path = rec.output / 'model_calls.jsonl'
        self.attempts = sum(1 for line in path.open(encoding='utf-8') if line.strip()) if path.exists() else 0

    def __enter__(self):
        from openai.resources.chat.completions import AsyncCompletions
        original = AsyncCompletions.create
        capture = self

        async def create(client, *args, **kwargs):
            if capture.attempts >= capture.max_calls:
                raise RuntimeError('benchmark_api_call_budget_exhausted')
            capture.attempts += 1
            started = time.perf_counter()
            metadata = dict(TRIAL.get())
            serial = json.dumps({k: kwargs.get(k) for k in ('model', 'messages', 'tools')}, ensure_ascii=False, default=str)
            row = dict(metadata, call_id=capture.attempts, started_at=now(), model=kwargs.get('model'),
                       request_sha256=hashlib.sha256(serial.encode()).hexdigest(), request_chars=len(serial),
                       **usage_fields(None))
            written = False
            def finish(error=None):
                nonlocal written
                if written:
                    return
                written = True
                row.update(elapsed_s=time.perf_counter()-started, error=safe_error(error) if error else None)
                capture.calls.append(row)
                capture.rec.append('model_calls.jsonl', row)
            try:
                response = await original(client, *args, **kwargs)
            except BaseException as exc:
                finish(exc)
                raise
            if kwargs.get('stream'):
                return ObservedStream(response, row, finish)
            row.update(usage_fields(response.usage))
            row['response_model'] = response.model
            finish()
            return response
        self.patch = patch.object(AsyncCompletions, 'create', create)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class ObservedStream:
    """Preserve AsyncStream's context-manager and iterator protocols."""
    def __init__(self, source, row, finish):
        self.source, self.row, self.finish = source, row, finish
        self.iterator = source.__aiter__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.source.close()
        self.finish(exc)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await anext(self.iterator)
        except StopAsyncIteration:
            self.finish()
            raise
        except BaseException as exc:
            self.finish(exc)
            raise
        if getattr(chunk, 'usage', None) is not None:
            self.row.update(usage_fields(chunk.usage))
        if getattr(chunk, 'model', None):
            self.row['response_model'] = chunk.model
        return chunk

    async def close(self):
        await self.source.close()
        self.finish()


_provider = None
_exported_outputs = set()


def install_tracing(rec):
    global _provider
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
    class Exporter(SpanExporter):
        def export(self, spans):
            for span in spans:
                rec.append('spans.jsonl', dict(TRIAL.get(), span=json.loads(span.to_json())))
            return SpanExportResult.SUCCESS
    if _provider is None:
        _provider = TracerProvider()
        trace.set_tracer_provider(_provider)
    if str(rec.output) not in _exported_outputs:
        processor = SimpleSpanProcessor(Exporter())
        _provider.add_span_processor(processor)
        _exported_outputs.add(str(rec.output))
    return trace.get_tracer('globex-benchmark')
