"""Paired real-provider measurement of existing context mechanisms; app unchanged."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from .common import create_run,finish,order,result,usage,failure
from scripts.benchmark.common import ROOT,TRIAL,digest,now,safe_error,write_json
from scripts.benchmark.observation import ApiCapture

ARMS=('full_history','summary_recent','summary_facts')
PROTOCOL=ROOT/'docs/context_benchmark_protocol.md'
PROBE=('只返回当前购物状态的JSON对象，不要解释。字段必须完整：budget(number), currency(ISO币种), '
       'country(ISO两位国家码), excluded_brands(当前品牌排除数组), excluded_types(当前类型排除数组), '
       'product_id(当前选中ID), quantity(number), pending_action(待买家确认用awaiting_confirmation、无待执行动作用none), '
       'selected_title(当前选中商品的完整标题)。当前明确修改优先于旧记录；临时例外仅影响适用期间。'
       '确实不知道的字段填null，不要猜。')


def evaluate(answer,expected):
    text=answer.strip()
    if text.startswith('```') and text.endswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    try:
        value=json.loads(text)
    except (ValueError,IndexError):
        return dict(schema_valid=False,fields={k:False for k in expected},parsed=None)
    schema=isinstance(value,dict) and set(value)==set(expected)
    fields={}
    for k,target in expected.items():
        actual=value.get(k) if isinstance(value,dict) else None
        if isinstance(target,list):
            aliases={'refurbished':'翻新商品','翻新':'翻新商品','翻新机':'翻新商品'} if k=='excluded_types' else {}
            normalized=[aliases.get(x.casefold(),x) for x in actual] if isinstance(actual,list) and all(isinstance(x,str) for x in actual) else None
            valid=normalized is not None and len(normalized)==len(set(normalized)) and set(normalized)==set(target)
        elif isinstance(target,(int,float)):
            valid=isinstance(actual,(int,float)) and not isinstance(actual,bool) and actual==target
        else:
            valid=type(actual) is type(target) and actual==target
        fields[k]=bool(valid)
    return dict(schema_valid=schema,fields=fields,parsed=value)


class SpendingGuard:
    """Conservative peak-price ceiling, including reserved unknown-usage attempts."""
    def __init__(self,capture,rec,limit,prior=0):
        self.capture,self.rec,self.limit,self.prior=capture,rec,limit,prior
        self.reservations={}
    def cost(self):
        total=self.prior
        for call in self.capture.calls:
            inp,out,cached=call.get('input_tokens'),call.get('output_tokens'),call.get('cached_tokens')
            if inp is None or out is None:
                total+=self.reservations[call['call_id']]
            else:
                cached=cached if cached is not None else 0
                total+=(cached*.04+(inp-cached)*2+out*8)/1e6
        return total
    def __enter__(self):
        from openai.resources.chat.completions import AsyncCompletions
        original=AsyncCompletions.create
        guard=self
        async def create(client,*args,**kwargs):
            payload={k:kwargs.get(k) for k in ('model','messages','tools','tool_choice','temperature','max_tokens','max_completion_tokens','extra_body','stream')}
            serial=json.dumps(payload,ensure_ascii=False,default=str)
            reserve=((len(serial.encode('utf-8'))+1024)*2+(kwargs.get('max_completion_tokens') or kwargs.get('max_tokens') or 2048)*8)/1e6
            if guard.cost()+reserve>guard.limit:
                raise RuntimeError('measurement_spending_limit_reached')
            call_id=guard.capture.attempts+1
            guard.reservations[call_id]=reserve
            artifact=guard.rec.artifact(f'request-{call_id:04}.json',payload)
            guard.rec.append('requests.jsonl',dict(TRIAL.get(),call_id=call_id,artifact=artifact,
                sha256=digest(guard.rec.output/artifact),reserved_peak_cny=reserve,timestamp=now()))
            return await original(client,*args,**kwargs)
        self.patch=patch.object(AsyncCompletions,'create',create)
        self.patch.start()
        return self
    def __exit__(self,*args):
        self.patch.stop()
        write_json(self.rec.output/'spending.json',dict(estimated_peak_cny_including_prior=self.cost(),prior_cny=self.prior,
            limit_cny=self.limit,not_actual_bill=True,missing_usage_reserved=True,
            pricing_url='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'))


def messages(history):
    from agentscope.message import Msg,UserMsg,TextBlock,ToolCallBlock,ToolResultBlock,ToolResultState
    out=[]
    for i,item in enumerate(history):
        if item['role']=='tool_fixture':
            key=f'fixture-{i}'
            out += [Msg(name='assistant',role='assistant',content=[ToolCallBlock(id=key,name=item['name'],input=json.dumps(item['input'],ensure_ascii=False))]),
                    Msg(name='tool',role='assistant',content=[ToolResultBlock(id=key,name=item['name'],state=ToolResultState.SUCCESS,output=json.dumps(item['output'],ensure_ascii=False))])]
        elif item['role']=='user':
            out.append(UserMsg(item['name'],item['content']))
        else:
            out.append(Msg(name=item['name'],role='assistant',content=[TextBlock(text=item['content'])]))
    return out


def model_and_agent(settings,case,variant):
    from agentscope.agent import Agent
    from agentscope.state import AgentState
    from agentscope.tool import Toolkit
    from app.infrastructure.llm import create_chat_model
    from app.application.agents.context_policy import build_context_config
    from app.application.agents.critical_facts import CriticalFactsMiddleware
    model=create_chat_model(replace(settings,llm_max_retries=0,llm_fallback_model='',context_size=4096,llm_min_interval_seconds=0),stream=True)
    model.max_retries=model.client.max_retries=0
    model.parameters.temperature=0
    model.parameters.max_tokens=2048
    model.client.timeout=__import__('httpx').Timeout(90)
    model.extra_body={'thinking':{'type':'disabled'}}
    agent=Agent(name='context_acceptance',system_prompt='你是购物助手。依据已提供的对话与工具事实回答；用户最新明确要求优先，缺少事实不要编造。',
                model=model,toolkit=Toolkit(tools=[]),state=AgentState(context=messages(case['history'])),
                context_config=build_context_config(4096,settings.tool_result_limit),
                middlewares=[CriticalFactsMiddleware()] if variant=='summary_facts' else [])
    return model,agent


async def run(rec,capture,cases,settings,args):
    from agentscope.message import UserMsg,Msg,TextBlock
    for repeat,case,variant in order(cases,ARMS):
        print('BEGIN '+case['id']+' '+variant,flush=True)
        model,agent=model_and_agent(settings,case,variant)
        started=time.perf_counter()
        first=len(capture.calls)
        rows=[]
        try:
            for turn,item in enumerate(case['continuations']):
                metadata=dict(suite='context_resume',case_id=case['id'],variant=variant,repeat=repeat,turn=turn)
                token=TRIAL.set(metadata)
                answer,error='',None
                tick=time.perf_counter()
                before_calls=len(capture.calls)
                summary_start=sum(c.get('phase')=='summary' for c in capture.calls)
                summary_before=agent.state.summary
                before_estimate=after_estimate=None
                agent.state.context.append(UserMsg('buyer',item['query']))
                try:
                    remaining=240-(time.perf_counter()-started)
                    if remaining<=0:
                        raise TimeoutError('case deadline exceeded')
                    async with asyncio.timeout(remaining):
                        initial=await agent._prepare_model_input()
                        before_estimate=await model.count_tokens(**initial)
                        TRIAL.set({**metadata,'phase':'summary'})
                        if variant!='full_history':
                            await agent.compress_context()
                        prepared=await agent._prepare_model_input()
                        after_estimate=await model.count_tokens(**prepared)
                        TRIAL.set({**metadata,'phase':'probe'})
                        stream=await model([*prepared['messages'],UserMsg('probe',PROBE)])
                        response=None
                        async for response in stream:
                            pass
                        answer='\n'.join(b.text for b in response.content if getattr(b,'type',None)=='text') if response else ''
                except Exception as exc:
                    error=safe_error(exc)
                assessment=evaluate(answer,item['expected'])
                calls=capture.calls[before_calls:]
                artifact=rec.artifact(f'{case["id"]}-{variant}-{turn}.json',dict(query=item['query'],expected=item['expected'],answer=answer,error=error,
                    assessment=assessment,summary=agent.state.summary,middle_context=agent.state.middle_context,
                    context=[m.model_dump(mode='json') for m in agent.state.context]))
                row=dict(**metadata,elapsed_s=time.perf_counter()-tick,success=not error and assessment['schema_valid'] and all(assessment['fields'].values()),
                    correct_fields=sum(assessment['fields'].values()),total_fields=len(item['expected']),
                    checks=assessment['fields'],schema_valid=assessment['schema_valid'],error=error,
                    failure_category=None,actual_compression=bool(agent.state.summary and agent.state.summary!=summary_before),
                    summary_calls=sum(c.get('phase')=='summary' for c in capture.calls)-summary_start,
                    before_estimated_tokens=before_estimate,after_estimated_tokens=after_estimate,
                    artifact=artifact,artifact_sha256=digest(rec.output/artifact),**usage(calls))
                if not row['success']:
                    row['failure_category']='measurement_budget' if error and 'spending_limit' in error else failure(error,answer) if error else 'schema' if not assessment['schema_valid'] else 'fact_mismatch'
                rows.append(row)
                rec.append('turns.jsonl',row)
                agent.state.context.append(Msg(name='assistant',role='assistant',content=[TextBlock(text=item['assistant_fixture'])]))
                TRIAL.reset(token)
                print(json.dumps(dict(case=case['id'],variant=variant,turn=turn,correct=row['correct_fields'],summary=row['summary_calls'],error=error)),flush=True)
        finally:
            await model.client.close()
        error_rows=[r for r in rows if not r['success']]
        result(rec,suite='context_resume',case_id=case['id'],variant=variant,repeat=repeat,
            elapsed_s=time.perf_counter()-started,success=not error_rows and len(rows)==len(case['continuations']),
            failure_category=error_rows[0]['failure_category'] if error_rows else None,
            successful_turns=sum(r['success'] for r in rows),planned_turns=len(case['continuations']),
            correct_fields=sum(r['correct_fields'] for r in rows),total_fields=sum(r['total_fields'] for r in rows),
            summary_calls=sum(r['summary_calls'] for r in rows),compressions=sum(r['actual_compression'] for r in rows),
            history_turns=case['prefix_turns'],tool_calls=0,**usage(capture.calls[first:]))


async def preflight(cases,settings):
    rows=[]
    for case in cases:
        for variant in ARMS:
            model,agent=model_and_agent(settings,case,variant)
            try:
                prepared=await agent._prepare_model_input()
                count=await model.count_tokens(**prepared)
                rows.append(dict(case_id=case['id'],variant=variant,estimated_tokens=count,threshold=3072))
            finally:
                await model.client.close()
    return rows


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--name',required=True)
    p.add_argument('--pilot',action='store_true')
    p.add_argument('--preflight',action='store_true')
    p.add_argument('--prior-cost',type=float,default=0)
    args=p.parse_args()
    from app.infrastructure.settings import load_settings
    settings=load_settings()
    dataset=ROOT/'benchmarks/datasets'/('context_resume_pilot.json' if args.pilot else 'context_resume_v1.json')
    cases=json.loads(dataset.read_text(encoding='utf-8'))
    if args.preflight:
        values=asyncio.run(preflight(cases,settings))
        write_json(ROOT/'benchmarks/aggregates/context_preflight.json',values)
        print(json.dumps(dict(cases=len(cases),min_tokens=min(x['estimated_tokens'] for x in values),max_tokens=max(x['estimated_tokens'] for x in values))))
        return
    config=dict(model=settings.llm_model,model_config=dict(temperature=0,max_tokens=2048,thinking='disabled',context_size=4096,trigger=.75,reserve=.15),
        pilot=args.pilot,planned_cases=len(cases),planned_arms=3,planned_probe_turns=sum(len(c['continuations']) for c in cases)*3,
        max_calls=400,max_peak_cny=3,prior_cost=args.prior_cost,protocol_sha256=digest(PROTOCOL),dataset_sha256=digest(dataset))
    rec,manifest=create_run(args.name,config)
    target=rec.output/'source/docs/context_benchmark_protocol.md'
    target.write_bytes(PROTOCOL.read_bytes())
    manifest['code_hashes']['docs/context_benchmark_protocol.md']=digest(PROTOCOL)
    write_json(rec.output/'manifest.json',manifest)
    logging.basicConfig(filename=rec.output/'runtime.log',level=logging.WARNING,encoding='utf-8')
    error=None
    try:
        with ApiCapture(rec,400) as capture, SpendingGuard(capture,rec,3,args.prior_cost):
            asyncio.run(run(rec,capture,cases,settings,args))
    except BaseException as exc:
        error=safe_error(exc)
        raise
    finally:
        finish(rec,manifest,error)


if __name__=='__main__':
    main()
