from benchmarks.runners.context_resume import evaluate


def test_state_scoring_rejects_missing_extra_fields_and_bool_as_number():
    expected={'budget':100,'excluded_brands':['A'],'pending_action':'none'}
    assert evaluate('{"budget":100,"excluded_brands":["A"],"pending_action":"none"}',expected)['schema_valid']
    assert not evaluate('{}',expected)['schema_valid']
    assert not evaluate('{"budget":100,"excluded_brands":["A"],"pending_action":"none","extra":1}',expected)['schema_valid']
    assert not evaluate('{"budget":true}',{'budget':1})['fields']['budget']


def test_new_intent_must_override_old_state_and_duplicates_are_not_valid():
    assert not evaluate('{"budget":300}',{'budget':120})['fields']['budget']
    assert not evaluate('{"excluded_brands":["A","A"]}',{'excluded_brands':['A']})['fields']['excluded_brands']
    assert evaluate('```json\n{"budget":120}\n```',{'budget':120})['fields']['budget']


def test_translation_of_refurbished_is_not_a_lost_exclusion():
    assert evaluate('{"excluded_types":["refurbished"]}',{'excluded_types':['翻新商品']})['fields']['excluded_types']
    assert not evaluate('{"excluded_types":["used"]}',{'excluded_types':['翻新商品']})['fields']['excluded_types']
