from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from jointctl.metadata_storage import MetadataStorage, MetadataStorageError, REMOTE_SCRIPT


@pytest.fixture
def transport(tmp_path):
    """Execute the actual robot receiver locally, replacing only POSIX locking
    and directory fsync on Windows, which cannot provide those Linux APIs.
    """
    remote = tmp_path / 'robot'
    requests = []

    def runner(argv, **kwargs):
        request = json.loads(kwargs.pop('input'))
        requests.append(request)
        request['root'] = str(remote)
        code = REMOTE_SCRIPT
        if os.name == 'nt':
            code = ("import sys,types\n"
                    "sys.modules['fcntl']=types.SimpleNamespace(LOCK_EX=2,flock=lambda *a:None)\n" + code)
            code = code.replace('def sync_dir(path):', "def sync_dir(path):\n    if os.name == 'nt': return")
        return subprocess.run([sys.executable, '-c', code], input=json.dumps(request), **kwargs)

    return remote, requests, runner


@pytest.fixture
def cache(tmp_path):
    folder = tmp_path / 'cache' / 'joint_test'
    folder.mkdir(parents=True)
    (folder/'manifest.json').write_text(json.dumps({
        'episode_id':folder.name,'created_desktop_ns':1,'label':'test','mode':'joint','state':'complete'}))
    for host in ('p450','unitree'):
        (folder/f'clock_{host}.jsonl').write_bytes(b'{"sequence":1}\n')
    return folder


def test_incremental_and_restarted_uploads_preserve_exact_evidence(cache, transport):
    remote, requests, runner = transport
    storage = MetadataStorage('robot-alias', runner=runner)
    storage.sync(cache)
    with (cache/'clock_p450.jsonl').open('ab') as f:
        f.write(b'{"sequence":2}\n')
    (cache/'raw_health.json').write_text('{"quality_ok":false}')
    (cache/'alignment.json').write_text('{"method":"clock"}')
    storage.sync(cache)
    p450 = next(r for r in requests[-1]['files'] if r['name']=='clock_p450.jsonl')
    assert p450['offset'] == len(b'{"sequence":1}\n')
    # A different computer/process starts at byte zero; identical evidence is
    # accepted without duplicate samples, while derived reports are retained.
    MetadataStorage('robot-alias', runner=runner).sync(cache, final=True)
    for path in cache.iterdir():
        if path.name != 'metadata_storage_status.json':
            assert path.read_bytes() == (remote/cache.name/path.name).read_bytes()
    assert json.loads((cache/'metadata_storage_status.json').read_text())['final'] is True


def test_ack_lost_after_remote_write_is_retryable_without_duplicate_records(cache, transport):
    remote, _, runner = transport
    lost = True

    def interrupted(argv, **kwargs):
        nonlocal lost
        result = runner(argv, **kwargs)
        if lost:
            lost = False
            raise subprocess.TimeoutExpired('ssh', 5)
        return result

    storage = MetadataStorage('unitree', runner=interrupted)
    with pytest.raises(MetadataStorageError, match='unavailable'):
        storage.sync(cache)
    assert json.loads((cache/'metadata_storage_status.json').read_text())['saved'] is False
    storage.sync(cache, final=True)
    assert (remote/cache.name/'clock_p450.jsonl').read_bytes() == (cache/'clock_p450.jsonl').read_bytes()


def test_different_remote_clock_evidence_is_never_overwritten(cache, transport):
    remote, _, runner = transport
    storage = MetadataStorage('unitree', runner=runner)
    storage.sync(cache)
    path = remote/cache.name/'clock_p450.jsonl'
    path.write_bytes(b'{"sequence":9}\n')
    with pytest.raises(MetadataStorageError, match='differs'):
        storage.sync(cache, final=True)  # Final check must reread the already-acknowledged prefix too.
    assert path.read_bytes() == b'{"sequence":9}\n'


def test_partial_local_line_is_buffered_and_final_upload_refuses_it(cache, transport):
    remote, _, runner = transport
    with (cache/'clock_p450.jsonl').open('ab') as f:
        f.write(b'{"sequence":')
    storage = MetadataStorage('unitree', runner=runner)
    storage.sync(cache)
    assert (remote/cache.name/'clock_p450.jsonl').read_bytes() == b'{"sequence":1}\n'
    with pytest.raises(MetadataStorageError, match='incomplete'):
        storage.sync(cache, final=True)
    with (cache/'clock_p450.jsonl').open('ab') as f:
        f.write(b'2}\n')
    storage.sync(cache, final=True)
    assert (remote/cache.name/'clock_p450.jsonl').read_bytes() == (cache/'clock_p450.jsonl').read_bytes()


def test_truncated_remote_tail_can_resume_identical_bytes(cache, transport):
    remote, _, runner = transport
    MetadataStorage('unitree', runner=runner).sync(cache)
    path = remote/cache.name/'clock_p450.jsonl'
    path.write_bytes(path.read_bytes()[:7])
    MetadataStorage('unitree', runner=runner).sync(cache, final=True)
    assert path.read_bytes() == (cache/'clock_p450.jsonl').read_bytes()


def test_final_sync_rejects_remote_extra_evidence_and_missing_local_clock(cache, transport):
    remote, _, runner = transport
    storage = MetadataStorage('unitree', runner=runner)
    storage.sync(cache)
    with (remote/cache.name/'clock_p450.jsonl').open('ab') as f:
        f.write(b'{"sequence":99}\n')
    with pytest.raises(MetadataStorageError):
        MetadataStorage('unitree', runner=runner).sync(cache, final=True)
    (cache/'clock_unitree.jsonl').unlink()
    with pytest.raises(MetadataStorageError, match='both clock'):
        storage.sync(cache, final=True)


def test_conflicting_episode_identity_is_refused(cache, transport):
    remote, _, runner = transport
    storage = MetadataStorage('unitree', runner=runner)
    storage.sync(cache)
    manifest = json.loads((cache/'manifest.json').read_text())
    manifest['created_desktop_ns'] = 2
    (cache/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(MetadataStorageError, match='identity conflict'):
        storage.sync(cache)
    assert json.loads((remote/cache.name/'manifest.json').read_text())['created_desktop_ns'] == 1


def test_fetch_to_another_computer_verifies_hashes_and_preserves_conflicts(cache, transport, tmp_path):
    _, _, runner = transport
    storage = MetadataStorage('unitree', runner=runner)
    storage.sync(cache, final=True)
    target = tmp_path/'other-computer'/cache.name
    storage.fetch(target)
    assert (target/'clock_p450.jsonl').read_bytes() == (cache/'clock_p450.jsonl').read_bytes()
    storage.fetch(target)  # Idempotent download.
    (target/'clock_p450.jsonl').write_text('conflicting local evidence')
    with pytest.raises(MetadataStorageError, match='local metadata differs'):
        storage.fetch(target)
    assert (target/'clock_p450.jsonl').read_text() == 'conflicting local evidence'


def test_fetch_missing_or_recording_episode_is_refused(cache, transport, tmp_path):
    remote, _, runner = transport
    storage = MetadataStorage('unitree', runner=runner)
    target = tmp_path/'new-cache'/'unknown'
    with pytest.raises(MetadataStorageError):
        storage.fetch(target)
    assert not target.exists() and not (remote/'unknown').exists()
    manifest = json.loads((cache/'manifest.json').read_text())
    manifest['state'] = 'recording'
    (cache/'manifest.json').write_text(json.dumps(manifest))
    storage.sync(cache)
    with pytest.raises(MetadataStorageError, match='stop the capture'):
        storage.fetch(tmp_path/'new-cache'/cache.name)


@pytest.mark.parametrize('root', ['relative/path','/robot/../other'])
def test_invalid_remote_roots_are_refused(root):
    with pytest.raises(ValueError):
        MetadataStorage('unitree', root)


@pytest.mark.parametrize('episode', ['../other','a/b','x;touch /tmp/pwned',''])
def test_unsafe_episode_ids_are_refused_before_transport(episode):
    storage = MetadataStorage('unitree', runner=lambda *_a, **_k:pytest.fail('must not run SSH'))
    with pytest.raises(ValueError):
        storage.remote_path(episode)


def test_monitor_upload_latency_does_not_block_either_probe(tmp_path):
    from jointctl.clock_sync import _run_monitor
    from tests.test_clock_sync import _sample
    counts = {'p450':0,'unitree':0}

    class Probe:
        def __init__(self, host): self.host = host
        def sample(self):
            counts[self.host] += 1
            return _sample(self.host, counts[self.host])
        def close(self): pass
        def drain_errors(self): return []

    class SlowStorage:
        def run(self, folder, stop):
            time.sleep(.3)  # Simulate a blocked remote fsync/upload.
            assert counts == {'p450':8,'unitree':8}
            assert stop.is_set()

    _run_monitor(tmp_path, ('p450','unitree'), .01, tmp_path/'clock.stop', Probe,
                 sample_count=8, metadata_storage=SlowStorage())
    assert all(len((tmp_path/f'clock_{h}.jsonl').read_text().splitlines())==8 for h in counts)


def test_unwritable_metadata_host_prevents_both_recorders_from_starting(tmp_path):
    from tests.test_start_controller import _Harness
    from jointctl.controller import JointStartFailed
    harness = _Harness(tmp_path)
    storage = MetadataStorage('unitree', runner=lambda *_a, **_k:
                              subprocess.CompletedProcess([], 255, '', 'offline'))
    harness.controller.metadata_storage = storage
    with pytest.raises(JointStartFailed) as error:
        harness.controller.start('test','joint')
    assert error.value.failure_kind == 'connectivity'
    assert harness.p450.start_calls == harness.unitree.start_calls == 0


@pytest.mark.parametrize('remote_ok', [True, False])
def test_stop_always_stops_recorders_then_requires_final_metadata_ack(tmp_path, transport, remote_ok):
    from tests.test_stop_recovery import _Harness
    _, _, runner = transport
    harness = _Harness(tmp_path/'local')
    folder = harness.store.episode_dir('joint_active')
    for host in ('p450','unitree'):
        (folder/f'clock_{host}.jsonl').write_bytes(b'{"sequence":1}\n')
    if not remote_ok:
        runner = lambda *_a, **_k: subprocess.CompletedProcess([],255,'','offline')
    harness.controller.metadata_storage = MetadataStorage('unitree', runner=runner)
    if remote_ok:
        manifest = harness.controller.stop()
        assert manifest.clock_monitor_closed_cleanly
        assert json.loads((folder/'metadata_storage_status.json').read_text())['final']
    else:
        with pytest.raises(MetadataStorageError):
            harness.controller.stop()
        assert harness.store.load('joint_active').state.value == 'complete'
        assert not json.loads((folder/'metadata_storage_status.json').read_text())['saved']
    assert harness.p450.stop_calls == harness.unitree.stop_calls == 1


def test_health_distinguishes_current_saved_failed_and_expired(cache):
    storage = MetadataStorage('unitree')
    assert storage.health_reason(cache, 100) is not None
    storage._status(cache, {'saved':True,'at_wall_ns':100,'error':None})
    assert storage.health_reason(cache, 101) is None
    assert storage.health_reason(cache, 20_000_000_100) is not None
    storage._status(cache, {'saved':False,'at_wall_ns':100,'error':'disk full'})
    assert 'disk full' in storage.health_reason(cache, 101)


def test_config_uses_unitree_alias_and_passes_storage_settings_to_detached_monitor(tmp_path):
    from argparse import Namespace
    from jointctl.cli import make_controller
    from jointctl.models import EpisodeManifest
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'unitree':{'host':'wired-robot'},
                                 'metadata_storage':{'root':'/robot/metadata','interval_s':3,'timeout_s':7}}))
    controller = make_controller(Namespace(config=config, manifest_root=tmp_path/'cache'))
    assert controller.metadata_storage.host == 'wired-robot'
    controller.store.create(EpisodeManifest('test','demo','joint',1))
    controller.monitor_launcher = lambda *_:123
    controller._launch_monitor('test')
    settings = json.loads((controller.store.episode_dir('test')/'clock_monitor_config.json').read_text())
    assert settings['metadata_storage'] == {'host':'wired-robot','root':'/robot/metadata','interval_s':3,'timeout_s':7}


def test_cli_failed_metadata_transfer_returns_failure_not_success(cache, tmp_path, monkeypatch, capsys):
    from jointctl.cli import main
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'metadata_storage':{}}))
    storage = MetadataStorage('unitree',runner=lambda *_a,**_k:
                              subprocess.CompletedProcess([],255,'','offline'))
    monkeypatch.setattr(MetadataStorage,'from_config',lambda _:storage)
    assert main(['--config',str(config),'--manifest-root',str(cache.parent),
                 'sync-metadata',cache.name]) == 3
    assert '本地缓存保留' in capsys.readouterr().err
