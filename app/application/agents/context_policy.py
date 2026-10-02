# -*- coding: utf-8 -*-
"""context_policy

Context 工程策略：把 2.0 内置的上下文压缩配置成跨境购物场景的口径
（即教程 Cache Breakpoint 章节要解决的问题——长对话不爆 token 且关键事实不丢）。

压缩触发：上下文占用达 context_size * trigger_ratio 时，Agent 自动把早期消息
压缩成摘要写入 AgentState.summary，保留末段 reserve_ratio 的原始消息。

关键取舍：摘要提示词显式列出"必须逐字保留"的事实清单（偏好、product_id/sku_id、
订单号与金额、待确认动作），避免压缩后 Agent 忘记已确认的商品或订单。

注意：summary_template 的占位符必须与 2.0 内置 summary_schema 的五个字段一致
（task_overview / current_state / important_discoveries / next_steps / context_to_preserve），
否则压缩时渲染摘要会抛 KeyError。
"""
from __future__ import annotations

from agentscope.agent import ContextConfig

_COMPRESSION_PROMPT = """<system-hint>当前对话上下文即将超出窗口，请把此前的工作压缩成一份中文摘要，
供你后续继续为这位买家服务。当前时间：{current_time}。

必须逐字保留的事实（丢失会导致后续回答出错）：
1. 买家的偏好与硬约束：材质忌口、风格取向、预算上限、收货国家/地址；
2. 已经推荐或买家已认可的商品：完整 product_id、sku_id、标题、价格与币种；
3. 订单相关：订单号、状态、总金额与币种、取消原因；
4. 当前待确认的动作：是否有等待买家确认的确认卡、下一步该做什么。

可以压缩或丢弃的内容：工具返回的完整候选列表（只留最终推荐项）、寒暄、重复表述、
已被更新覆盖的中间结论。

预算、数量等用户要求必须忠实保留用户原话中的数值；价格、库存、订单金额必须来自工具返回。
同一诉求有更新时只把最新要求作为当前状态；取消的待确认动作必须标为已取消，不能再次执行。
历史偏好不作为当前偏好的权威来源，每轮以独立注入的最新偏好快照为准。
不得重新估算数字，不得把历史记录当作购买授权。</system-hint>"""

_COMPRESSION_PROMPT += """
输出是一份紧凑的事实快照，不是工作报告或操作手册。以约500个中文字为目标；
若关键标识和未解决约束确实放不下，可以超出，不能为了缩短而遗漏事实。
每个事实只出现一次，放进最合适的字段；不要在五个字段中重复预算、商品和待确认状态。
不要逐轮引用买家原话，不要复述系统规则、工具名称、工具参数写法、权限或未来操作教程。
next_steps 只写买家已经明确要求且尚未完成的事项，无则写“无”；不得新增收货地址、确认卡、
重新搜索等未经请求的计划。取消购买与清除商品选择是不同动作，只记录实际发生的结果，
不要自行指示后续清除字段或修改状态。context_to_preserve 只补充前四项未覆盖且继续任务必需的事实，
无补充写“无”。已覆盖的旧要求不再列为当前约束。"""

_SUMMARY_TEMPLATE = """<system-info>历史事实摘要；当前用户要求、结构化购物状态和最新偏好快照优先。
需求：{task_overview}
状态：{current_state}
商品及订单事实：{important_discoveries}
未完成事项：{next_steps}
其他必要事实：{context_to_preserve}</system-info>"""

# The framework's generic schema asks for technical decisions, failed approaches,
# artifacts and plans. Those descriptions inflate shopping summaries and can
# invent follow-up work. Use the same five keys with domain-specific semantics.
_SUMMARY_FIELDS = {
    'task_overview': '只写当前商品需求、最新预算及币种、目的地和仍生效的约束；不写变更历程。',
    'current_state': '只写实际购买/订单状态和明确的待确认或已取消动作；不复制商品详情或预算。',
    'important_discoveries': '每件需保留的商品/订单用一条紧凑记录，包含完整ID、标题、工具原价/币种及必要金额；只在此处列这些事实。',
    'next_steps': '仅记录用户明确要求且尚未完成的事项；没有就写无，禁止推演后续操作。',
    'context_to_preserve': '仅补充未在前四项出现的必要事实；没有就写无，不复述规则、原话或偏好快照。',
}


def build_context_config(context_size: int, tool_result_limit: int) -> ContextConfig:
    """构造 Mercurius 的上下文压缩策略。

    Args:
        context_size (`int`):
            模型上下文窗口大小（与 create_chat_model 保持一致）。
        tool_result_limit (`int`):
            单个工具结果的字符上限，超出会被截断，防止商品卡 JSON 挤爆上下文。
    """
    del context_size  # 窗口由 model 侧提供，这里仅保留参数以标明配套关系
    return ContextConfig(
        trigger_ratio=0.75,
        reserve_ratio=0.15,
        compression_prompt=_COMPRESSION_PROMPT,
        summary_template=_SUMMARY_TEMPLATE,
        summary_schema={
            'type': 'object',
            'properties': {key: {'type': 'string', 'description': description}
                           for key, description in _SUMMARY_FIELDS.items()},
            'required': list(_SUMMARY_FIELDS),
            'additionalProperties': False,
        },
        tool_result_limit=tool_result_limit,
    )
