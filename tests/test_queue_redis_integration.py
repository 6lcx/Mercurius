"""Opt-in real Redis >=6.2 integration; owns unique keys and never flushes a database."""
import asyncio
import os
import uuid
import pytest

@pytest.mark.asyncio
async def test_real_redis_recovers_abandoned_large_task(monkeypatch):
    url=os.environ.get('GLOBEX_TEST_REDIS_URL')
    if not url:
        pytest.skip('Set GLOBEX_TEST_REDIS_URL to an isolated Redis >=6.2')
    import redis.asyncio as redis
    import app.infrastructure.queue.redis_stream_queue as module
    from app.domain.queue.ports.task_queue import IntentTask
    prefix='globex-test:'+uuid.uuid4().hex
    keys=[prefix+suffix for suffix in [':normal',':large',':dead']]
    for name,key in zip(['_STREAM','_LARGE_STREAM','_DEAD_STREAM'],keys):
        monkeypatch.setattr(module,name,key)
    monkeypatch.setattr(module,'_STATUS_PREFIX',prefix+':status:')
    client=redis.from_url(url,decode_responses=True)
    queue=module.RedisStreamTaskQueue(client)
    stopped=False
    handled=[]
    try:
        await queue.ensure_group()
        await queue.enqueue(IntentTask('recovered','session','buyer','zh-CN','CNY','offline',priority=1))
        abandoned=await client.xreadgroup(module._GROUP,'dead-worker',{keys[1]:'>'},count=1)
        message_id=abandoned[0][1][0][0]
        await client.xclaim(keys[1],module._GROUP,'dead-worker',0,[message_id],idle=61000)
        async def handle(task):
            nonlocal stopped
            handled.append(task.task_id)
            stopped=True
        await asyncio.wait_for(queue.consume('replacement-worker',handle,lambda: stopped,block_ms=1),5)
        assert handled == ['recovered']
        pending=await client.xpending(keys[1],module._GROUP)
        assert pending['pending'] == 0
        assert await client.xlen(keys[2]) == 0
    finally:
        await client.delete(*keys,prefix+':status:recovered')
        await client.aclose()
