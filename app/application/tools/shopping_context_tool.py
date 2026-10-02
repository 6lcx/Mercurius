"""Explicit current shopping state and request-scoped preference exceptions.

The model interprets language; the server validates provenance and scope. This
tool records intent, never authorizes a purchase or mutates a long-term dislike.
"""
import json
import math
import re
from typing import Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.infrastructure.context import ShoppingContext


def _result(payload, error=False):
    return ToolChunk(content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))],
                     state=ToolResultState.ERROR if error else ToolResultState.SUCCESS)


def build_update_shopping_context_tool():
    async def update_shopping_context_tool(
        evidence: str,
        price_max_major: float | None = None,
        target_currency: str | None = None,
        ship_to: str | None = None,
        product_id: str | None = None,
        sku_id: str | None = None,
        pending_action: Literal['awaiting_confirmation', 'cancelled', 'none'] | None = None,
        clear_fields: list[str] | None = None,
        new_task: bool = False,
    ) -> ToolChunk:
        """记录当前购物诉求；用户更新预算、选中商品、取消待确认动作时先调用。

        evidence 必须逐字引用本轮用户原话。仅记录明确表达的要求，不从旧摘要推断。
        price_max_major 是预算，target_currency 是三字母币种，ship_to 是两字母国家码。
        product_id/sku_id 必须来自用户原话或已查询的商品。
        pending_action 表示待确认/取消/无待办，不代表用户已经授权下单。
        clear_fields 显式清除字段；换独立购物任务时 new_task=true 清空旧任务要求。
        不同并行子任务的局部约束直接传给子任务，不写成全局购物状态。
        """
        ctx = ShoppingContext.current()
        if ctx is None or ctx.session_data is None:
            return _result({'error': 'shopping_session_unavailable'}, True)
        if not evidence.strip() or evidence not in ctx.raw_query:
            return _result({'error': 'evidence_must_quote_current_user'}, True)
        fields = {'price_max_major', 'target_currency', 'ship_to', 'product_id', 'sku_id', 'pending_action'}
        if set(clear_fields or ()) - fields:
            return _result({'error': 'unknown_clear_field'}, True)
        if price_max_major is not None and (not math.isfinite(price_max_major) or price_max_major < 0):
            return _result({'error': 'invalid_budget'}, True)
        if price_max_major is not None:
            numbers = [float(n) for n in re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', evidence, flags=re.ASCII)]
            if price_max_major not in numbers:
                return _result({'error': 'budget_must_match_quoted_number',
                                'notice': '预算必须匹配本轮原话中的阿拉伯数字，无法确定时先澄清。'}, True)
        if target_currency is not None and (len(target_currency) != 3 or not target_currency.isalpha()):
            return _result({'error': 'invalid_currency'}, True)
        if ship_to is not None and (len(ship_to) != 2 or not ship_to.isalpha()):
            return _result({'error': 'invalid_country'}, True)
        if pending_action not in {None, 'awaiting_confirmation', 'cancelled', 'none'}:
            return _result({'error': 'invalid_pending_action'}, True)
        known = json.dumps(ctx.session_data.get('critical_entities', {}), ensure_ascii=False)
        for identifier in (product_id, sku_id):
            if identifier and identifier not in ctx.raw_query and json.dumps(identifier, ensure_ascii=False) not in known:
                return _result({'error': 'unknown_product_identifier'}, True)
        previous = ctx.session_data.get('current_shopping', {})
        state = {} if new_task else dict(previous)
        for field in clear_fields or ():
            state.pop(field, None)
        # A changed product invalidates its previous SKU and pending confirmation.
        if product_id is not None and product_id != previous.get('product_id'):
            state.pop('sku_id', None)
            state['pending_action'] = 'none'
        updates = dict(price_max_major=price_max_major, target_currency=target_currency,
                       ship_to=ship_to, product_id=product_id, sku_id=sku_id, pending_action=pending_action)
        state.update({k: v.upper() if k in {'target_currency', 'ship_to'} else v
                      for k, v in updates.items() if v is not None})
        if price_max_major is not None and 'target_currency' not in state:
            state['target_currency'] = ctx.currency
        state.update(revision=previous.get('revision', 0) + 1, evidence=evidence)
        ctx.session_data['current_shopping'] = state
        return _result({'current_shopping': state, 'purchase_authorized': False})

    return update_shopping_context_tool


def build_allow_preference_once_tool(store):
    async def allow_preference_once_tool(statement: str, evidence: str) -> ToolChunk:
        """买家明确要求本轮例外时，暂停一条长期负向偏好，仅本轮有效，绝不删除。

        statement 必须照抄长期偏好原文；evidence 必须逐字引用本轮用户表达例外的原话。
        例如长期“不喜欢红色商品”，本轮“这次可以接受红色”。不得为凑商品擅自放宽。
        下一轮自动恢复；搜索子 Agent 在本轮自动继承例外。
        """
        ctx = ShoppingContext.current()
        if ctx is None or not evidence.strip() or evidence not in ctx.raw_query:
            return _result({'error': 'evidence_must_quote_current_user'}, True)
        # Fail closed for negated/ambiguous exceptions. This intentionally only
        # accepts explicit forms; more language coverage needs labelled cases.
        lower = evidence.lower()
        temporary = any(s in lower for s in ('这次', '这一次', '本次', '本轮', '此次', 'this time'))
        affirmative = any(s in lower for s in ('可以接受', '可接受', '可以用', '允许', '不介意', 'can accept'))
        ambiguous = any(s in lower for s in ('不可以', '不能', '不允许', '不接受', '不要', '是否', '能否', '吗', '?', '？', 'cannot', "can't"))
        # Check only the quoted exception clause, not a separate "不要删除" clause.
        target = re.sub(r'^(?:不喜欢|不要|排除|避开|material:|brand:|color:)\s*', '', statement, flags=re.I)
        target = re.sub(r'(?:材质|品牌|的?商品)$', '', target).strip()
        if not temporary or not affirmative or ambiguous or not target or target.casefold() not in lower:
            return _result({'error': 'explicit_temporary_exception_required',
                            'notice': '请引用明确的本次允许条件，不能引用否定、疑问或删除指令。'}, True)
        try:
            rows = await store.list_by_buyer(ctx.buyer_id)
        except Exception:
            return _result({'error': 'preference_store_unavailable'}, True)
        if not any(p.kind == 'dislike' and p.statement == statement for p in rows):
            return _result({'error': 'exact_dislike_not_found'}, True)
        ctx.turn_data.setdefault('preference_exceptions', {})[statement] = evidence
        return _result({'temporarily_suspended': statement, 'scope': 'current_user_turn',
                        'long_term_preference_deleted': False})

    return allow_preference_once_tool
