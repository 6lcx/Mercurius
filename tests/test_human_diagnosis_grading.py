"""Synthetic unit fixtures only; these are NOT human observations."""
import pytest
from scripts.benchmark.grade_human_diagnosis import grade


KEY={'D01':dict(request_id='r',component='model',category='rate_limit')}


def fixture(**changes):
    answer=dict(case_id='D01',request_id='r',condition='logs',component='model',category='rate_limit',elapsed_ms=1000,hidden_ms=0)
    answer.update(changes)
    return dict(participant='synthetic-unit-fixture',group='A',protocol_hash='test-only',attestation=True,answers=[answer])


def test_grading_keeps_incorrect_answers_and_does_not_synthesize_missing_participants():
    report=grade([fixture(category='unknown')],KEY,'test-only')
    assert report['summary']['logs']['observations']==1
    assert report['summary']['logs']['correct']==0
    assert report['summary']['logs']['median_s_correct'] is None
    assert not report['both_groups_present']
    assert report['summary']['assisted']['observations']==0


@pytest.mark.parametrize('changes',[{'elapsed_ms':None},{'elapsed_ms':float('nan')},{'hidden_ms':2000},{'condition':'assisted'}])
def test_missing_or_inconsistent_records_cannot_be_silently_scored(changes):
    with pytest.raises(ValueError):grade([fixture(**changes)],KEY,'test-only')


def test_tutorial_revision_is_not_pooled_with_original_interface():
    original=fixture()
    guided=fixture()
    guided.update(participant='another-synthetic-fixture',interface_version='guided-zh-v2')
    with pytest.raises(ValueError,match='interface versions'):
        grade([original,guided],KEY,'test-only')
    assert grade([guided],KEY,'test-only')['interface_version']=='guided-zh-v2'
