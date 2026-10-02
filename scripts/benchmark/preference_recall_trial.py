"""Frozen, age-balanced preference selection evaluation with real local BGE."""
import asyncio
import json
from pathlib import Path

from .common import digest, write_json


TOPICS = [
    ('camping', '周末露营需要照明设备，帮我选灯', ['露营灯喜欢USB-C充电接口', '露营灯喜欢整晚持续照明的长续航款']),
    ('luggage', '出差乘飞机想买一个登机箱', ['行李箱喜欢20英寸硬壳款', '旅行箱喜欢静音万向轮']),
    ('audio', '通勤地铁上听音乐用什么耳机', ['耳机喜欢主动降噪功能', '耳机喜欢包耳式佩戴']),
    ('bedding', '想换卧室的床单和被套', ['床上用品喜欢纯棉面料', '被套床单喜欢可以机洗的']),
    ('coffee', '给家里的咖啡机买点豆子', ['咖啡豆喜欢浅度烘焙', '咖啡豆喜欢带水果酸香的风味']),
    ('tent', '两人去野外过夜，需要帐篷', ['帐篷喜欢双人空间', '帐篷喜欢防暴雨的外帐']),
    ('backpack', '徒步一整天，买个背东西的包', ['登山背包喜欢轻量款', '徒步背包喜欢有支撑背板的']),
    ('shoes', '想买一双户外登山鞋', ['登山鞋喜欢宽鞋头', '户外鞋喜欢防滑耐磨鞋底']),
    ('watch', '游泳和跑步都戴的运动手表', ['运动手表喜欢支持游泳防水的', '手表喜欢持续监测心率功能']),
    ('keyboard', '办公桌需要一个打字键盘', ['键盘喜欢安静的轴体', '键盘喜欢紧凑小尺寸布局']),
    ('camera', '旅游时想用相机记录风景', ['相机喜欢轻便可随身携带的', '相机喜欢可更换镜头的']),
    ('cycling', '夜间骑自行车买什么装备', ['骑行灯喜欢高亮度照明', '骑行头盔喜欢轻量透气款']),
]


async def main():
    from app.application.memory.preference_selector import PreferenceSelector
    from app.domain.buyer.preference import BuyerPreference
    from app.infrastructure.embedding.local_embedding import LocalEmbeddingClient

    root = Path('output/metric-targets/preference-recall-v1')
    root.mkdir(parents=True, exist_ok=False)
    preferences = []
    cases = []
    for i, (topic, query, statements) in enumerate(TOPICS):
        ids = []
        for statement in statements:
            pid = f'p{len(preferences):02d}'
            ids.append(pid)
            preferences.append(dict(id=pid, kind='like', statement=statement,
                created_at=f'2026-09-{len(preferences)+1:02d}T00:00:00+00:00'))
        cases.append(dict(id=topic, query=query, relevant=ids, age_stratum=('old', 'middle', 'recent')[i//4]))
    for i, statement in enumerate(['不要真皮材质', '不要红色商品', '不要一次性电池供电的产品']):
        preferences.append(dict(id=f'n{i}', kind='dislike', statement=statement, created_at='2026-08-01T00:00:00+00:00'))
    write_json(root/'protocol.json', dict(preferences=preferences, cases=cases, top_k=5,
        scope='Synthetic development selection benchmark, not actual user traffic or end-to-end recommendation accuracy',
        baseline='Most recent five positive preferences', optimized='Production BGE relevance selector',
        metrics=['macro recall@5 of two labeled positive preferences per query', 'all negative preferences retained'],
        age_balance='All 24 positive preferences labeled once; four queries per age stratum',
        target=.72))
    write_json(root/'source-hashes.json', {str(p):digest(p) for p in [Path(__file__), Path('app/application/memory/preference_selector.py')]})
    values = [BuyerPreference('benchmark', p['kind'], p['statement'], p['created_at']) for p in preferences]
    identifiers = {p['statement']:p['id'] for p in preferences}
    negative_ids = {p['id'] for p in preferences if p['kind']=='dislike'}
    embedder = LocalEmbeddingClient()
    rows = []
    for enabled in (False, True):
        selector = PreferenceSelector(embedder, relevance_enabled=enabled)
        for case in cases:
            selected = await selector.select(values, case['query'], 5)
            ids = [identifiers[p.statement] for p in selected]
            rows.append(dict(case_id=case['id'], age_stratum=case['age_stratum'],
                variant='relevance' if enabled else 'recency', selected=ids,
                recall=len(set(ids)&set(case['relevant']))/len(case['relevant']),
                all_negatives_retained=negative_ids <= set(ids)))
            write_json(root/'results.json', rows)
    summary = {}
    for variant in ('recency', 'relevance'):
        group = [r for r in rows if r['variant']==variant]
        summary[variant] = dict(recall_at_5=sum(r['recall'] for r in group)/len(group),
            full_positive_recall_cases=sum(r['recall']==1 for r in group), cases=len(group),
            negative_retention=sum(r['all_negatives_retained'] for r in group)/len(group),
            by_age={age:sum(r['recall'] for r in group if r['age_stratum']==age)/4 for age in ('old','middle','recent')})
    write_json(root/'summary.json', summary)
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
