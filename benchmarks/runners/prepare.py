"""Build synthetic fixtures and a searchable whole-repository audit inventory."""
import ast
import json
from pathlib import Path
from scripts.benchmark.common import ROOT, digest, now, write_json


def prepare():
    context = []
    for i, length in enumerate((15, 20, 30)):
        expected = dict(budget=150+i*100, country='CN', excluded_brand='BrandZ',
                        excluded_type='翻新商品', product_id='P1008', pending_action='待确认', quantity=1)
        turns = []
        for turn in range(length):
            query = f'第{turn+1}轮，我们讨论旅行收纳；请介绍衣物整理方法，不改变购物要求。'
            tool_input = None
            if turn == 0:
                query = (f'预算{expected["budget"]}元，寄中国CN，排除BrandZ品牌和翻新商品。'
                         '我选中P1008，数量1，购买动作待确认，不要下单。')
                tool_input = dict(normalized_query='露营灯', price_max_major=expected['budget'],
                                  ship_to='CN', excluded_brands=['BrandZ'])
            if i > 0 and turn == 6:
                expected = {**expected, 'budget':120+i*10, 'country':'US', 'quantity':2}
                query = f'修改本次预算为{expected["budget"]}元，目的地改为美国US，数量改为2；其他条件不变，仍待确认。'
                tool_input = dict(normalized_query='露营灯', price_max_major=expected['budget'],
                                  ship_to='US', excluded_brands=['BrandZ'])
            # Explicitly synthetic exposition, same bytes in every arm. Not LLM output.
            answer = ('旅行衣物可以按使用场景分袋整理，出发前查看气候、行李限重和清洁标签。'
                      '把常用物品放在容易取用的位置，保持干燥，返程重新检查物品清单。')*5
            turns.append(dict(query=query, assistant_fixture=answer, tool_input=tool_input,
                              tool_result=dict(product_id='P1008',title='LumenGo 便携露营灯 可充电',
                                               price_major=89,currency='CNY') if tool_input else None,
                              expected=dict(expected)))
        context.append(dict(id=f'context-{length}', length=length, origin='Codex synthetic', turns=turns))
    isolation = []
    for kind in ('clean','irrelevant','revoked','omitted'):
        for product, name, budget in (('P1008','露营灯',150),('P1005','充电器',200)):
            parent = [dict(role='user',content=f'当前选购{name}，预算{budget}元含运费，收货中国CN。')]
            if kind == 'irrelevant':
                for j in range(8):
                    parent += [dict(role='user',content=f'另外请讲解第{j}种行李打包方式。'),
                               dict(role='assistant',content='衣物按使用时间分袋整理，保持干燥，检查目的地气候。'*20)]
            if kind == 'revoked':
                parent = [dict(role='user',content='旧需求预算20元，收货美国，不要露营灯或充电器。'),
                          dict(role='assistant',content='旧需求暂存。'),*parent]
            demand = (f'旧的临时要求作废。当前请检索{name}，预算{budget}元含运费，寄中国CN。'
                      '调用商品工具，返回商品ID和到手价，不下单。')
            if kind == 'omitted':
                demand = f'根据已经给出的购物要求检索{name}，返回商品ID和到手价，不下单。'
            isolation.append(dict(id=f'{kind}-{product}',kind=kind,target=product,budget=budget,
                                  country='CN',parent=parent,demand=demand,origin='Codex synthetic'))
    write_json(ROOT/'benchmarks/datasets/context.json',context)
    write_json(ROOT/'benchmarks/datasets/isolation.json',isolation)
    inventory=[]
    for folder in ('app','tests','scripts','docs','eval','knowledge','frontend/src','docker'):
        for path in sorted((ROOT/folder).rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix not in ('.py','.md','.yaml','.yml','.jsonl','.ts','.tsx','.css'):
                continue
            content=path.read_text(encoding='utf-8-sig')
            row=dict(path=path.relative_to(ROOT).as_posix(),sha256=digest(path),lines=len(content.splitlines()))
            if path.suffix=='.py':
                tree=ast.parse(content)
                row['definitions']=[dict(name=n.name,line=n.lineno) for n in ast.walk(tree)
                                    if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))]
                row['test_double_markers']={s:content.lower().count(s) for s in ('mock','fake','stub','monkeypatch','skipif')}
            inventory.append(row)
    write_json(ROOT/'benchmarks/aggregates/audit_inventory.json',dict(created_at=now(),files=inventory,
        scope='Whole text inventory and Python AST scan; focused source review in architecture audit. Not a claim of exercising every path.'))
    print(json.dumps(dict(context_cases=len(context),context_probe_calls=sum(c['length'] for c in context)*4,
                         isolation_cases=len(isolation),inventory_files=len(inventory))))


if __name__=='__main__':
    prepare()
