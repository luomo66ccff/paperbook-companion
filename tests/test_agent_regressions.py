import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests

import ai_story_generate as stories
import text_agent_base as agents
from nlu import QAAssistant


class Response:
    def __init__(self, content):
        self.content = content
    def raise_for_status(self):
        pass
    def json(self):
        return {'choices': [{'message': {'content': self.content}}]}


def test_first_transport_failure_recovers_and_stateless_calls_do_not_share_history(monkeypatch):
    payloads = []
    def post(*args, **kwargs):
        payloads.append(kwargs['json'])
        if len(payloads) == 1:
            raise requests.Timeout('controlled timeout')
        return Response('ok')
    monkeypatch.setattr(agents.requests, 'request', post)
    agent = agents.Gemini3ProAgent('http://localhost/text', 'test', 'controlled', system_prompt=None)
    assert agent.call('first', max_try=2) == ('ok', True)
    assert agent.last_attempts == 2
    assert agent.call('second', max_try=2) == ('ok', True)
    assert payloads[-1]['messages'] == [{'role': 'user', 'content': 'second'}]
    assert payloads[-1]['max_tokens'] == 512


def test_invalid_structure_and_time_budget_are_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(agents.requests, 'request', lambda *a, **k: (calls.append(k), Response('invalid'))[1])
    agent = agents.Gemini3ProAgent('http://localhost/text', 'test', 'controlled')
    result, success = agent.call('question', max_try=2, success_check_fn=lambda _: False)
    assert result == 'invalid' and not success and len(calls) == 2
    clock = iter([0, 0, 2])
    monkeypatch.setattr(agents, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    calls.clear()
    agent.retry_budget = 1
    assert not agent.call('question', max_try=10, success_check_fn=lambda _: False)[1]
    assert len(calls) == 1


def test_history_flag_is_respected_and_failures_do_not_append_partial_turns(monkeypatch):
    payloads = []
    monkeypatch.setattr(agents.requests, 'request', lambda *a, **k: (payloads.append(k['json']), Response('ok'))[1])
    agent = agents.Gemini3ProAgent('http://localhost/text', 'test', 'controlled', track_history=True)
    agent.call('one')
    agent.call('two')
    assert [m['role'] for m in payloads[-1]['messages']] == ['user', 'assistant', 'user']
    prior = list(agent.history)
    agent.call('bad', max_try=1, success_check_fn=lambda _: False)
    assert agent.history == prior


def test_model_output_is_never_executed(tmp_path):
    marker = tmp_path / 'should-not-exist'
    malicious = f"[__import__('pathlib').Path({str(marker)!r}).write_text('bad')]"
    assert not stories.parse_list(malicious)
    assert not marker.exists()
    assert stories.parse_story_pages('```json\n["first", "second"]\n```') == ['first', 'second']
    for malformed in ('[]', '[1]', '["ok", {}]', '{}'):
        assert not stories.parse_list(malformed)


def test_deep_chapter_failure_stops_without_outer_infinite_retry(monkeypatch):
    calls = []
    class Failure:
        def __init__(self, **kwargs):
            pass
        def call(self, *args, **kwargs):
            calls.append(kwargs)
            return '', False
    monkeypatch.setattr(stories, 'Gemini3ProAgent', Failure)
    writer = stories.GeminiAIStroyWriter('http://localhost/text', 'test', 'controlled')
    with pytest.raises(RuntimeError, match='chapter 1'):
        writer.generate_story_from_outline({'story_outline': [{'chapter_title': 'one', 'chapter_summary': 'summary'}]},
                                           {'language': '中文', 'knowledge_points': ''})
    assert len(calls) == 1 and 'seed' not in calls[0]


def test_env_storage_root_is_loaded_before_configuration(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'app' / 'config.py'
    (tmp_path / 'config.py').write_bytes(source.read_bytes())
    target = tmp_path / 'data'
    (tmp_path / '.env').write_text('TARGET_DIR=' + str(target) + '\n', encoding='utf-8')
    import os
    environment = dict(os.environ)
    environment.pop('TARGET_DIR', None)
    environment.pop('PAPERBOOK_SKIP_DOTENV', None)  # this test loads its own temporary .env on purpose
    result = subprocess.run([sys.executable, '-c',
                             'import sys;sys.path.insert(0,sys.argv[1]);import config;print(config.TARGET_DIR)', str(tmp_path)],
                            env=environment, capture_output=True, text=True, encoding='utf-8', check=True)
    assert Path(result.stdout.strip()) == target


def test_question_context_and_history_reach_text_service_and_invalid_citations_fail(monkeypatch):
    payloads = []
    answer = ['It shares apples [p0001].']
    def post(*args, **kwargs):
        payloads.append(kwargs['json'])
        return Response(answer[0])
    monkeypatch.setattr(agents.requests, 'request', post)
    qa = QAAssistant('http://localhost/text', 'test', 'controlled')
    context = [{'page_id': 'p0001', 'page_num': 1, 'excerpt': 'Rabbit shares apples.'}]
    history = [{'question': 'Who shares apples?', 'answer': 'Rabbit [p0001].'}]
    assert qa.call('What does it share?', context=context, history=history).endswith('[p0001].')
    sent = json.loads(payloads[-1]['messages'][-1]['content'])
    assert sent['evidence'] == context and sent['history'] == history
    answer[0] = 'On Mars [p9999].'
    with pytest.raises(ValueError, match='页引用'):
        qa.call('Where?', context=context)
