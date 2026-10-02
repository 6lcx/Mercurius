"""Frozen synthetic histories. No LLM and no business writes during construction."""
import copy
import json
from scripts.benchmark.common import ROOT, write_json

TOPICS=[
 ('衣物收纳','衣物按用途分别收纳，常用物品放在方便拿取的位置。干湿物品分开，检查洗涤标签，避免潮湿环境。'),
 ('出发检查','出发前核对证件有效期、目的地天气和行李限制。纸质与电子清单分别保存，返程也检查随身物品。'),
 ('装备保养','使用之后清除表面泥沙，在阴凉处晾干。检查接缝与拉链，不要自行拆卸有电池的设备。'),
 ('雨天安排','雨天给衣物增加防水袋，在安全处收纳潮湿物品。不要将防水等级理解成适合所有水下活动。'),
 ('行程安排','行程预留休息时间，区分必要停留与可选活动。遇到天气变化优先调整路线，保留通信和返程安排。'),
 ('包装材料','重复利用状态良好的包装，拆包后核对配件和说明。标签应与内部物品对应，不根据包装推断产品性能。'),
 ('充电管理','按照设备说明选择充电方式，检查接口和额定参数。不同型号的兼容性需要分别核实，不只看接头外形。'),
 ('清单管理','将随身、托运和到达后购买的物品分开列出。勾选实际装入的物品，避免把计划中购买的东西当作已经拥有。')]
KINDS=['stable_lamp','stable_charger','stable_backpack','budget_replace','country_replace','product_replace',
       'brand_withdraw','type_withdraw','temporary_brand','intent_withdraw','multiple_revision','distractor_numbers']


def build_case(kind,index,products):
    pid=['P1008','P1005','P1001'][index%3]
    product=products[pid]
    expected=dict(budget=300+index*20,currency='CNY',country='CN',excluded_brands=['BrandZ'],
        excluded_types=['翻新商品'],product_id=pid,quantity=1,pending_action='awaiting_confirmation',selected_title=product.title)
    prefix_turns=(15,20,24)[index%3]
    initial=(f'本次商品预算{expected["budget"]}元人民币CNY，寄中国CN，排除BrandZ品牌和翻新商品。'
             f'选中{pid}，商品标题是“{product.title}”，数量1。购买动作待我确认，尚未下单。')
    history=[dict(role='user',name='buyer',content=initial),
        dict(role='tool_fixture',name='product_search_tool',input=dict(normalized_query=product.title,
             ship_to='CN',target_currency='CNY',price_max_major=expected['budget'],excluded_brands=['BrandZ']),
             output=dict(hits=[dict(product_id=pid,title=product.title,sku_id=product.skus[0].sku_id,
                                   price_major=product.skus[0].price.to_major_units(),currency=product.skus[0].price.currency)],
                         recall_strategy='synthetic_fixture_from_seed_product')),
        dict(role='assistant',name='assistant',content='已展示商品信息，等待买家确认。')]
    for turn in range(1,prefix_turns):
        topic,body=TOPICS[(turn+index)%len(TOPICS)]
        history += [dict(role='user',name='buyer',content=f'先谈{topic}，本次只咨询常识，不改变选品条件。'),
                    dict(role='assistant',name='assistant',content=(body+'上述建议用于旅行准备，具体做法以产品说明及实际情况为准。')*4)]
    continuations=[]
    for step in range(6):
        query='继续刚才的购物事项；这轮没有修改任何条件。'
        if step==1:
            if kind=='budget_replace':
                expected['budget']=180;query='预算改为180元人民币，之前的预算作废，其他不变。'
            elif kind=='country_replace':
                expected['country']='US';query='收货国家改为美国US，其他条件不变，预算仍以人民币计。'
            elif kind=='product_replace':
                new=products['P1039'];expected.update(product_id='P1039',selected_title=new.title,quantity=2)
                query=f'改选P1039，标题“{new.title}”，数量改为2，之前的商品不选了，仍待确认。'
            elif kind=='brand_withdraw':
                expected['excluded_brands']=[];query='撤销对BrandZ的排除，本次不再排除任何品牌；翻新商品仍不要。'
            elif kind=='type_withdraw':
                expected['excluded_types']=[];query='翻新商品现在可以接受，删除本次全部商品类型排除条件；品牌限制不变。'
            elif kind=='temporary_brand':
                expected['excluded_brands']=[];query='这次送礼可以接受BrandZ，本次品牌排除为空，但保留平时不喜欢BrandZ的长期偏好。'
            elif kind=='intent_withdraw':
                expected['pending_action']='none';query='先不买了，撤回待确认的购买动作。商品选择和预算保留用于以后比较，但当前没有待执行动作。'
            elif kind=='multiple_revision':
                expected.update(budget=210,country='US',quantity=2);query='预算改为210元人民币，寄美国US，数量2。其他不变。'
            elif kind=='distractor_numbers':
                query='另一趟旅行机票花了900元，酒店住3晚，朋友住在美国；这些都不改变我的商品预算、数量和收货地。'
        if step==3 and kind=='temporary_brand':
            expected['excluded_brands']=['BrandZ'];query='刚才送礼的临时例外结束，当前选品恢复排除BrandZ，其他条件不变。'
        if step==3 and kind=='multiple_revision':
            expected.update(budget=190,country='CN',quantity=1);query='再次更新：预算190元人民币，改回中国CN，数量恢复1；此前中间条件作废。'
        continuations.append(dict(query=query,expected=copy.deepcopy(expected),
                                  assistant_fixture='本轮信息已记录；尚未执行任何订单操作。'))
    return dict(id=kind,origin='Codex synthetic',prefix_turns=prefix_turns,total_turns=prefix_turns+6,
                history=history,continuations=continuations)


async def prepare():
    from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
    repo=InMemoryProductRepository()
    products={p.product_id:p for p in await repo.list_all()}
    cases=[build_case(kind,i,products) for i,kind in enumerate(KINDS)]
    pilot=build_case('pilot',12,products)
    pilot['continuations']=pilot['continuations'][:1]
    write_json(ROOT/'benchmarks/datasets/context_resume_v1.json',cases)
    write_json(ROOT/'benchmarks/datasets/context_resume_pilot.json',[pilot])
    return cases


if __name__=='__main__':
    import asyncio
    print('Frozen cases:',len(asyncio.run(prepare())))
