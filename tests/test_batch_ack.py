import importlib.util
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('summon_batch', ROOT / 'bin/summon-skills.py')
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, 'session_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(ss, 'load_routes', lambda: ([], []))
    monkeypatch.setattr(ss, 'load_index', lambda **kw: {})
    monkeypatch.setattr(ss, 'suggest', lambda prompt, *args: ([(prompt, 'test')], []))
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text('')
    return {'session_id': 'test', 'transcript_path': str(transcript), 'tool_name': 'Bash'}


def prompt(payload, skill):
    return ss.hook_prompt(dict(payload, prompt=skill))


def test_each_real_prompt_batch_denies_once(setup):
    prompt(setup, 'first:one')
    assert ss.hook_pretool(setup)
    assert not ss.hook_pretool(setup)
    prompt(setup, 'second:two')
    assert ss.hook_pretool(setup)
    assert not ss.hook_pretool(setup)


def test_parallel_calls_deny_only_once(setup):
    prompt(setup, 'first:one')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ss.hook_pretool(setup), range(8)))
    assert sum(bool(r) for r in results) == 1


def test_late_old_save_does_not_replace_new_batch(setup, monkeypatch):
    prompt(setup, 'first:one')
    old = ss._load_barrier('test')
    prompt(setup, 'second:two')
    old['pending'] = {}
    ss._save_barrier('test', old)
    assert ss.hook_pretool(setup)
    assert ss._load_barrier('test')['pending'] == {'second:two': 'test'}


def test_old_call_finishes_before_new_prompt_updates_state(setup, monkeypatch):
    prompt(setup, 'first:one')
    entered, release = Event(), Event()
    original = ss._visible_mentions

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(ss, '_visible_mentions', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(ss.hook_pretool, setup)
        assert entered.wait(5)
        new = pool.submit(prompt, setup, 'second:two')
        release.set()
        assert old.result(timeout=5)
        assert new.result(timeout=5)
    assert ss.hook_pretool(setup)
    assert ss._load_barrier('test')['pending'] == {'second:two': 'test'}


def test_legacy_barrier_remains_readable(setup):
    ss._save_barrier('test', {'pending': {'old:skill': 'test'}, 'denied': False})
    assert ss.hook_pretool(setup)
    assert not ss.hook_pretool(setup)


def test_late_legacy_save_does_not_replace_new_batch(setup):
    ss._save_barrier('test', {'pending': {'old:skill': 'test'}, 'denied': False})
    old = ss._load_barrier('test')
    prompt(setup, 'second:two')
    old['pending'] = {}
    ss._save_barrier('test', old)
    assert ss.hook_pretool(setup)
    assert ss._load_barrier('test')['pending'] == {'second:two': 'test'}


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('text,want', [
    ('Using second:one', {'second:one'}),
    ('Using one', set()),
    ('Using first:one-extra', set()),
    ('Using lone', {'third:lone'}),
    ('Using lone.', {'third:lone'}),
])
def test_unambiguous_name_matching(setup, native, text, want):
    if native:
        rec = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': [{'type': 'output_text', 'text': text}]}}
    else:
        rec = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}
    Path(setup['transcript_path']).write_text(json.dumps(rec) + '\n')
    pending = {'first:one': 'test', 'second:one': 'test', 'third:lone': 'test'}
    assert ss._visible_mentions(setup, pending) == want
