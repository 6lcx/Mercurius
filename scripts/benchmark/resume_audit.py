"""Build the public claim index from existing immutable local measurements."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def build():
    evidence = {
        'context': 'output/resume-verification-v4/context/summary.json',
        'context_protocol': 'output/resume-verification-v4/context/protocol.json',
        'parallel': 'output/resume-verification-v4/parallel/results.jsonl',
        'faults': 'output/resume-verification-v3/faults/results.jsonl',
        'retrieval': 'output/resume-verification-v2/local/aggregate.json',
        'category': 'output/resume-verification-v2/category/results.json',
        'web_before': 'output/resume-verification-v5/web-complete/summary.json',
        'web_after': 'output/resume-verification-v6/web/summary.json',
        'regression': 'output/resume-verification-v6/pytest.xml',
    }
    rows = lambda path: [json.loads(s) for s in Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]
    context = read(evidence['context'])
    assert context['passed_cases'] == context['independent_cases'] == 6
    assert context['passed_turns'] == context['turns'] == 12
    parallel = rows(evidence['parallel'])
    assert len(parallel) == 6 and all(r['success'] for r in parallel)
    faults = rows(evidence['faults'])
    recoverable = {'transient_once', 'persistent_transient', 'pre_stream_once'}
    recovery = {v: {'recovered': sum(r['success'] for r in faults if r['variant'] == v and r['case_id'] in recoverable),
                    'trials': sum(r['variant'] == v and r['case_id'] in recoverable for r in faults)}
                for v in ('protection_off', 'protection_on')}
    retrieval = [g for g in read(evidence['retrieval'])['groups'] if g['suite'] == 'retrieval']
    scores = {g['variant']: g['metrics']['recall']['mean'] for g in retrieval}
    suite = ET.parse(evidence['regression']).getroot().find('testsuite')
    assert int(suite.get('failures')) == int(suite.get('errors')) == 0
    claims = []

    def claim(original, resolution, implementation, verification, note=''):
        claims.append({'original': original, 'resolution': resolution, 'implementation': implementation,
                       'verification': verification, 'scope_or_reason': note})

    claim('跨平台搜索、比价与筛选', '支持跨来源候选检索与价格比较',
          ['app/infrastructure/catalog/external_discovery.py', 'app/application/usecases/product_recommendation.py'],
          ['web_before', 'web_after', 'tests/test_external_discovery.py'],
          '同型号灯曾取得两个商家报价；最新3类需求2类有候选，分类页不冒充商品；未知运税不补零。')
    claim('多轮理解偏好、输出推荐理由', '保留能力描述',
          ['app/application/agents/orchestrator.py', 'app/application/prompts/globex.yml'], ['context'],
          '真实模型及业务状态验收；不是所有用户表达均保证正确。')
    claim('AgentScope ReAct，主Agent处理简单请求', '保留',
          ['app/application/agents/main_agent.py'], ['context', 'tests/test_product_pipeline_wiring.py'])
    claim('Task工具管理复杂任务并派发专家', '保留',
          ['app/application/agents/main_agent.py', 'app/application/tools/task_dispatch_tool.py'], ['parallel'])
    claim('独立子任务上下文，减少无关历史干扰', '改为独立对话上下文，不宣称准确率提升',
          ['app/application/tools/task_dispatch_tool.py'], ['parallel', 'tests/test_subagent_preference_inject.py'],
          '每次build独立Agent，只传demands与服务端偏好；必要业务状态经ContextVar共享。')
    claim('支持并发执行', '保留并行能力，3类双专家任务验收',
          ['app/application/agents/main_agent.py'], ['parallel'],
          '本次延迟测量与本地测试有短暂重叠，不写加速百分比。')
    claim('压缩历史、保留近期消息', '保留',
          ['app/application/agents/context_policy.py'], ['context', 'tests/test_phase3.py'],
          '生产trigger_ratio=.75、reserve_ratio=.15；验收强制压缩，不代表自动触发率。')
    claim('摘要保留预算、商品标识、待确认动作', '改为摘要结合结构化状态保存',
          ['app/application/agents/critical_facts.py', 'app/application/tools/shopping_context_tool.py'],
          ['context', 'tests/test_context_repair.py'], '不是仅靠摘要提示词逐字保留。')
    claim('独立注入长期偏好', '保留',
          ['app/application/agents/orchestrator.py', 'app/application/tools/task_dispatch_tool.py'],
          ['context', 'tests/test_subagent_preference_inject.py'])
    claim('完整保留排除条件', '改为注入时完整保留已存负向偏好',
          ['app/application/memory/preference_selector.py', 'app/application/usecases/product_recommendation.py'],
          ['context', 'tests/test_context_repair.py'], '任意自然语言排除不是全覆盖；无法解析时要求澄清。')
    claim('按需选取正向偏好', '保留相关性选择',
          ['app/application/memory/preference_selector.py', 'app/infrastructure/settings.py'],
          ['output/context-repair-v1/production-permissions-v1/real-embedding-selection.json'],
          '真实BGE选择案例通过；不将单例说成用户偏好命中率72%。')
    claim('跨会话复用与删除', '保留，补充当轮例外',
          ['app/application/tools/forget_preference_tool.py', 'app/application/tools/shopping_context_tool.py'],
          ['context', 'tests/test_memory_persistence.py', 'tests/test_context_repair.py'])
    claim('Prompt Cache稳定80%', '删除稳定80%，只保留稳定system前缀设计',
          ['app/application/agents/critical_facts.py'],
          ['output/context-repair-v1/production-permissions-v1/summary.json'],
          '该6轮观测缓存输入Token占比约72.29%；缓存占比与请求命中率、费用和延迟不同。')
    claim('分级超时与熔断', '保留', ['app/infrastructure/resilience.py'],
          ['faults', 'tests/test_phase3.py'], '冷却计时评测已修正，原边界失败记录保留。')
    claim('模型瞬时故障重试、备用模型回退', '保留，以3类故障注入恢复60/60对照0/60量化',
          ['app/infrastructure/llm.py'], ['faults'],
          '人工注入限流、持续暂态失败、输出前断流；永久错误和输出后断流不强制重试。')
    claim('任务派发、偏好管理重复调用提醒', '保留提醒，不写保证自愈',
          ['app/application/harness/loop_detector.py', 'app/infrastructure/harness_middleware.py'],
          ['tests/test_harness_guards.py', 'tests/test_harness_middleware.py'])
    claim('任务完成率71%到89%', '用明确的组件故障恢复指标替换', [], ['faults'],
          '不支持原端到端完成率；不把注入恢复率叫线上任务完成率。')
    claim('OpenTelemetry与业务事件追踪', '保留请求与工具事件关联',
          ['app/infrastructure/tracing.py', 'app/infrastructure/eventbus.py'],
          ['tests/test_order_queue_regressions.py', 'output/benchmarks/live-consented-v3/aggregate.json'],
          '本地Span与业务事件关联已验证；不宣称已验证完整外部OTLP后端。')
    claim('问题定位30分钟到5分钟', '删除分钟收益', [], [], '无真人盲测计时数据。')
    claim('参考模板：复杂任务RT降低65%', '不采用65%',
          ['app/application/tools/task_dispatch_tool.py'], ['parallel'], '保留并行机制与完成证据。')
    claim('参考模板：10+轮Token降低30%', '不采用该数值，不把压缩直接当降本提速',
          ['app/application/agents/context_policy.py'], ['context'],
          '旧合成历史实验有输入缩减，但成本和延迟未同步改善；未混作当前完整链路因果收益。')
    claim('参考模板：长期记忆命中72%、历史选择复用', '保留持久化能力，去掉未测命中率',
          ['app/application/agents/critical_facts.py', 'app/application/memory/preference_selector.py'], ['context'])
    claim('参考模板：Embedding+BM25，Top100相关率+22%，跨语言+35%',
          '改为实际规则评分+BGE，67条开发集平均Recall@8相对提升28.9%',
          ['app/application/usecases/product_recommendation.py'], ['retrieval'],
          '当前主流程无BM25与reranker；不将旧召回路径和当前服务混淆。跨语言35%未证实。')
    claim('参考模板：LangFuse全链路Trace', '使用当前OpenTelemetry名称',
          ['app/infrastructure/tracing.py'], ['tests/test_order_queue_regressions.py'])
    claim('参考模板：6种事件+WebSocket，重复提交0%', '实际13种事件，保留会话路由能力，删除用户行为收益',
          ['app/infrastructure/eventbus.py', 'app/presentation/server.py'],
          ['tests/test_order_queue_regressions.py'],
          '订单幂等测试不等于用户重复提交率；不把自定义事件说成已验证完整AG-UI标准兼容。')
    claim('参考模板：日均8万用户、超越闭源模型', '不采用', [], [], '无生产流量或同口径模型对照证据。')
    claim('补充：品类RAG评测', '22条开发问题，Top3片段后文档Recall97.73%',
          ['app/infrastructure/rag/category_knowledge.py', 'app/application/tools/category_insight_tool.py'], ['category'],
          '5篇静态文档；不是生成回答准确率或知识时效保证。')
    for c in claims:
        for p in c['implementation'] + c['verification']:
            assert Path(evidence.get(p, p)).is_file(), p
    result = {'scope': 'Original 3-bullet resume plus every claim in the reference template; revised wording is the deliverable',
              'claims': claims, 'measurements': {'context': context, 'recovery': recovery, 'recall_at_8': scores,
                  'relative_recall_improvement': scores['rules_embedding']/scores['rules']-1,
                  'category': {k:v for k,v in read(evidence['category']).items() if k != 'per_query'},
                  'web': read(evidence['web_after']),
                  'regression': dict(suite.attrib)},
              'evidence': {name: {'path':path, 'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                           for name,path in evidence.items()},
              'excluded_claims_are_not_implemented': True,
              'raw_evidence_is_local': True,
              'raw_evidence_note': 'Raw API/web responses remain local; public summaries do not include credentials or full merchant pages.'}
    Path('docs/resume-claim-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print({'audited_claims': len(claims), 'relative_recall_improvement': result['measurements']['relative_recall_improvement']})


if __name__ == '__main__':
    build()
