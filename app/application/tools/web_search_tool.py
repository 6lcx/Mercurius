# -*- coding: utf-8 -*-
"""Web evidence discovery: only gated, persisted evidence reaches the agent."""
import json

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.settings import Settings


def build_web_search_tool(settings: Settings, bus: TradeEventBus, service=None):
    async def web_search_tool(query: str, max_results: int = 3) -> ToolChunk:
        """搜索网页正文，经本地知识阈值与降权筛选后入库并返回证据。

        找外部商品时，query 必须保留购买意图、完整型号和用户约束。
        商品页须有价格及购买入口证据；不返回测评文章或不匹配型号。
        返回购买链接不代表已确认库存、运费或配送范围。

        Args:
            query (`str`): 自然语言检索问题。
            max_results (`int`): 最终知识片段数，默认 3，最多 5。
        """
        session_id = ShoppingContext.current_session_id()
        bus.publish(session_id, "tool.invoke", {"tool": "web_search_tool", "args": {"query": query}})
        try:
            if service is None:
                raise RuntimeError("网页知识准入服务未装配，无法提供未经筛选的网页内容")
            payload = await service.search(query, top_k=max(1, min(int(max_results), 5)), discover=True)
        except Exception as err:
            bus.publish(session_id, "tool.result", {"tool": "web_search_tool", "error": str(err)})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] web 搜索失败：{err}")],
                state=ToolResultState.ERROR,
            )
        bus.publish(session_id, "tool.result", {"tool": "web_search_tool", "hit_count": len(payload["insights"])})
        return ToolChunk(
            content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
            state=(ToolResultState.ERROR if payload.get("gate", {}).get("status") == "local_unavailable"
                   else ToolResultState.SUCCESS),
        )

    return web_search_tool
