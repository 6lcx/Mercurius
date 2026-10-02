"""Real Redis Lua concurrency checks. All keys are unique and deleted afterward."""
import asyncio
import os
import uuid
import pytest

@pytest.mark.asyncio
async def test_concurrent_failures_are_not_lost():
    endpoint=os.environ.get('GLOBEX_TEST_REDIS_URL')
    if not endpoint: pytest.skip('Set GLOBEX_TEST_REDIS_URL for real atomicity verification')
    from app.infrastructure.cache.redis_cache import RedisCache
    from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
    cache=RedisCache(endpoint)
    name='counter-test-'+uuid.uuid4().hex
    registries=[SharedCircuitBreakerRegistry(cache,failure_threshold=20,reset_seconds=60) for _ in range(20)]
    try:
        await asyncio.gather(*(registry.record_failure_async(name,now=100) for registry in registries))
        state=await cache.get_json('globex:breaker:'+name)
        assert state['failures'] == 20
        assert await registries[0].allow_async(name,now=101) is False
    finally:
        await cache.delete('globex:breaker:'+name)
        await cache.delete('globex:breaker:'+name+':probe')
        await cache.close()

@pytest.mark.asyncio
async def test_expired_probe_cannot_mutate_new_owner_state():
    endpoint=os.environ.get('GLOBEX_TEST_REDIS_URL')
    if not endpoint: pytest.skip('Set GLOBEX_TEST_REDIS_URL for real ownership verification')
    from app.infrastructure.cache.redis_cache import RedisCache
    from app.infrastructure.shared_breaker import SharedCircuitBreakerRegistry
    cache=RedisCache(endpoint)
    name='owner-test-'+uuid.uuid4().hex
    breaker=SharedCircuitBreakerRegistry(cache,failure_threshold=1,reset_seconds=1)
    try:
        await breaker.record_failure_async(name,now=10)
        assert await breaker.allow_async(name,now=12)
        # Simulate expiration/replacement while this task is still running.
        await cache.client.set('globex:breaker:'+name+':probe','new-owner',ex=60)
        before=await cache.get_json('globex:breaker:'+name)
        await breaker.record_failure_async(name,now=13)
        assert await cache.client.get('globex:breaker:'+name+':probe') == 'new-owner'
        assert await cache.get_json('globex:breaker:'+name) == before
    finally:
        await cache.delete('globex:breaker:'+name)
        await cache.delete('globex:breaker:'+name+':probe')
        await cache.close()
