import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from result_transport import read_final_reply
from test_parallel_processes import setup_job, execute
from parallel_analysis import read_worker_runtime
import codex_run as r


@pytest.mark.parametrize('value', [
    [{'marker_id': 'm'}], {'marker_id': 'm'},
    {'decisions': [{'marker_id': 'm'}], 'source_requests': [{'file_path': 'a.c', 'reason': 'guard'}]},
])
def test_final_json_is_only_transport(tmp_path, value):
    reply=tmp_path/'reply.txt'
    reply.write_text(json.dumps(value))
    rows, requests = read_final_reply(reply, ['m'])
    assert rows == [{'marker_id': 'm'}]  # deliberately not a valid decision
    assert requests == (value.get('source_requests', []) if isinstance(value, dict) else [])


@pytest.mark.parametrize('text', [
    'Finished, result is []', '[]', '{"marker_id":"other"}',
    '[{"marker_id":"m"},{"marker_id":"m"}]', '{"decisions":[{"marker_id":"m"}],"source_requests":"bad"}',
])
def test_wrong_or_prose_final_is_not_accepted(tmp_path,text):
    reply=tmp_path/'reply.txt'
    reply.write_text(text)
    assert read_final_reply(reply,['m']) is None
    assert read_final_reply(tmp_path/'not-created.txt',['m']) is None


def test_readonly_worker_can_return_json_without_writing_result(tmp_path,monkeypatch):
    job,ctx=setup_job(tmp_path,monkeypatch,'final_reply')
    assert execute(job,ctx)==(0,0)
    for mid,row in read_worker_runtime(job)['workers'].items():
        directory=(job/row['event_log']).parent
        assert len(list(directory.glob('prompt-*.txt')))==1
        saved=r.read_json(directory/'context.json')
        result=r.read_json(Path(saved['result_file']))
        assert result[0]['marker_id']==mid
        assert row['state']=='incomplete' and not row['error']
        assert result[0]['proof_gaps']==['missing real evidence']
