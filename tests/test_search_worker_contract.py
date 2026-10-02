import json
from types import SimpleNamespace

import pytest
from agentscope.message import AssistantMsg, ToolResultState
from app.application.tools.task_dispatch_tool import SearchWorkerResult, build_task_dispatch_tool
from app.infrastructure.eventbus import TradeEventBus


@pytest.mark.asyncio
async def test_dispatch_uses_validated_metadata_instead_of_unstructured_body():
    async def reply(inputs, structured_schema=None):
        assert structured_schema is SearchWorkerResult
        return AssistantMsg('worker','This is not JSON',structured_output={'hits':[{'product_id':'P1'}],'notes':'from tools'})
    factory=SimpleNamespace(build=lambda:SimpleNamespace(reply=reply))
    result=await build_task_dispatch_tool(factory,factory,TradeEventBus())('search_agent','find a lamp')
    assert result.state==ToolResultState.SUCCESS
    assert json.loads(result.content[0].text)=={'hits':[{'product_id':'P1'}],'notes':'from tools'}


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[None,{'hits':'invented','notes':'bad'}, {'hits':[]}])
async def test_dispatch_reports_missing_or_invalid_schema_as_error(payload):
    async def reply(inputs,structured_schema=None):
        return AssistantMsg('worker','Pretend success',structured_output=payload)
    factory=SimpleNamespace(build=lambda:SimpleNamespace(reply=reply))
    result=await build_task_dispatch_tool(factory,factory,TradeEventBus())('search_agent','find a lamp')
    assert result.state==ToolResultState.ERROR
