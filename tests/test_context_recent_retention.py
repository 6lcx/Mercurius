"""Exercise real AgentScope splitting with production reserve policy, no paid API."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from agentscope.message import Msg, UserMsg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from app.application.agents.context_policy import build_context_config


@pytest.mark.asyncio
async def test_compression_preserves_latest_budget_and_complete_tool_exchange(monkeypatch):
    model=OpenAIChatModel(model='test-model',credential=OpenAICredential(api_key='test',base_url='http://127.0.0.1:9'),stream=False)
    policy=build_context_config(8192,20000)
    old=[]
    for i in range(16):
        old.extend([UserMsg('buyer',f'第{i}次旧需求：预算300元。'),
                    Msg(name='assistant',role='assistant',content=[TextBlock(text='已记录旧需求。'+('这些是此前的选购讨论。'*25))])])
    recent=[UserMsg('buyer','预算改成90元，取消待确认购买。'),
            Msg(name='assistant',role='assistant',content=[ToolCallBlock(id='latest',name='update_shopping_context_tool',input='{"price_max_major":90,"pending_action":"cancelled"}')]),
            Msg(name='tool',role='assistant',content=[ToolResultBlock(id='latest',name='update_shopping_context_tool',state=ToolResultState.SUCCESS,
                output='{"current_shopping":{"price_max_major":90,"pending_action":"cancelled"}}')])]
    agent=Agent(name='recent_retention',system_prompt='购物助手',model=model,toolkit=Toolkit(tools=[]),
                context_config=policy,state=AgentState(context=old+recent))
    prepared=await agent._prepare_model_input()
    count=await model.count_tokens(**prepared)
    model.context_size=int(count/.8)
    summary=AsyncMock(return_value=SimpleNamespace(finished_reason=None,content={k:'历史购物讨论' for k in policy.summary_schema['required']}))
    monkeypatch.setattr(model,'generate_structured_output',summary)
    try:
        await agent.compress_context()
        assert summary.await_count==1
        assert 0<len(agent.state.context)<len(old+recent)
        assert [m.model_dump() for m in agent.state.context[-3:]]==[m.model_dump() for m in recent]
    finally:
        await model.client.close()
