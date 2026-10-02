import json
import pytest
from scripts.benchmark.acceptance_review import resolve_pointer, validate_review
from scripts.benchmark.common import digest, write_json
from scripts.benchmark.shopping_acceptance import load_cases


def test_frozen_dataset_has_coverage_and_complete_confirmation():
    cases=load_cases()
    assert sum(c['mode']=='web' for c in cases)==6
    assert len(next(c for c in cases if c['id']=='simulated-order-cycle')['turns'])==4
    assert sum(len(c['criteria']) for c in cases)==72


def test_review_rejects_fabricated_quote_and_does_not_call_refusal_fulfillment(tmp_path):
    artifact=tmp_path/'case.json'
    write_json(artifact,{'case':{'criteria':['price evidence']},'turns':[{'text':'运费未知，不能确认总价。'}]})
    row={'case_id':'c','artifact':'case.json','artifact_sha256':digest(artifact)}
    (tmp_path/'observations.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
    criterion={'index':0,'status':'insufficient_evidence','explanation':'没有运费报价',
               'evidence':[{'pointer':'/turns/0/text','quote':'运费未知'}]}
    judgment={**row,'delivery':'unfulfilled','criteria':[criterion]}
    review={'reviewer':{'kind':'AI','name':'Codex','independent':False,'human_reviewed':False},'cases':[judgment]}
    assert validate_review(tmp_path,review)['c']==row
    judgment['delivery']='fulfilled'
    with pytest.raises(ValueError,match='Fulfilled'):
        validate_review(tmp_path,review)
    judgment['delivery']='unfulfilled'
    criterion['evidence'][0]['quote']='已确认包邮'
    with pytest.raises(ValueError,match='Unsupported quotation'):
        validate_review(tmp_path,review)


def test_json_pointer_handles_escaped_keys():
    assert resolve_pointer({'a/b':{'~': [7]}},'/a~1b/~0/0')==7
