import importlib.util
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

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


def test_each_real_prompt_batch_reminds_at_most_twice(setup):
    prompt(setup, 'first:one')
    out1 = ss.hook_pretool(setup)
    out2 = ss.hook_pretool(setup)
    out3 = ss.hook_pretool(setup)
    for out in (out1, out2):
        assert "summon-skills reminder" in out and "additionalContext" in out, out
        assert "permissionDecision" not in out, out
    assert out3 == "", out3   # third call on the same ignored batch goes silent


def test_new_batch_resets_reminder_budget(setup):
    prompt(setup, 'first:one')
    assert ss.hook_pretool(setup)
    assert ss.hook_pretool(setup)
    assert ss.hook_pretool(setup) == ""
    prompt(setup, 'second:two')   # a new prompt batch gets its own reminder budget
    out = ss.hook_pretool(setup)
    assert "summon-skills reminder" in out and "permissionDecision" not in out, out
    assert "summon-skills reminder" in ss.hook_pretool(setup)
    assert ss.hook_pretool(setup) == ""


def test_parallel_calls_never_exceed_two_reminders(setup):
    prompt(setup, 'first:one')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ss.hook_pretool(setup), range(8)))
    non_empty = [r for r in results if r]
    assert len(non_empty) == 2, results
    for r in non_empty:
        assert "summon-skills reminder" in r and "permissionDecision" not in r, r


def test_pretool_never_emits_permission_decision(setup):
    prompt(setup, 'first:one')
    outs = [ss.hook_pretool(setup) for _ in range(4)]
    prompt(setup, 'second:two')
    outs += [ss.hook_pretool(setup) for _ in range(3)]
    outs.append(ss.hook_pretool(dict(setup, tool_name='Skill',
                                     tool_input={'skill': 'second:two'})))
    for out in outs:
        assert "permissionDecision" not in out, out


def test_legacy_denied_state_never_denies(setup):
    # legacy on-disk state: "denied": true plus an old .<batch>.deny.json claim file.
    # It stays readable, never causes a deny, and the batch still gets at most 2 reminders.
    ss._save_barrier('test', {'pending': {'old:skill': 'test'}, 'denied': True,
                              'batch': 'legacybatch'}, new_batch=True)
    claim = ss._barrier_file('test').with_name(
        ss._barrier_file('test').name.replace('.barrier.json', '.legacybatch.deny.json'))
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.touch()
    outs = [ss.hook_pretool(setup) for _ in range(3)]
    assert "summon-skills reminder" in outs[0], outs[0]
    assert "summon-skills reminder" in outs[1], outs[1]
    assert outs[2] == "", outs[2]
    for out in outs:
        assert "deny" not in out, out


def test_other_session_progresses_while_first_is_locked(setup, monkeypatch):
    prompt(setup, 'first:one')
    entered, release = Event(), Event()
    original = ss._visible_mentions

    def paused(payload, pending):
        if payload['session_id'] == 'test':
            entered.set()
            assert release.wait(5)
        return original(payload, pending)

    monkeypatch.setattr(ss, '_visible_mentions', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ss.hook_pretool, setup)
        try:
            assert entered.wait(5)
            other = dict(setup, session_id='other')
            second = pool.submit(prompt, other, 'second:two')
            assert second.result(timeout=1)
        finally:
            release.set()
        assert first.result(timeout=5)


def test_same_session_lock_wait_is_bounded_and_fails_open(setup, monkeypatch):
    prompt(setup, 'first:one')
    entered, release = Event(), Event()
    original = ss._visible_mentions

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(ss, '_visible_mentions', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ss.hook_pretool, setup)
        try:
            assert entered.wait(5)
            start = time.monotonic()
            assert ss.hook_pretool(setup) == ''
            assert time.monotonic() - start < 2
        finally:
            release.set()
        assert first.result(timeout=5)


def test_stale_lock_cleanup_preserves_locked_session(setup):
    prompt(setup, 'first:one')
    path = ss.session_dir() / 'test.lock'
    old = time.time() - ss.STATE_TTL - 1
    with ss._session_lock('test'):
        import os
        os.utime(path, (old, old))
        ss.prune_sessions()
        assert path.exists()
    ss.prune_sessions()
    assert not path.exists()


def test_no_flock_keeps_cli_usable_and_hooks_fail_open(setup, monkeypatch, capsys):
    monkeypatch.setattr(ss, 'fcntl', None)
    with pytest.raises(SystemExit) as result:
        ss.main(['--version'])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == '0.2.6'
    assert ss.hook_pretool(setup) == ''


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
