import copy
import http.client
import json
import threading
import time

import pytest

from jointctl.task_marker_service import TaskMarkerService, capture_context


def observations():
    return {'p450': {'value': {}, 'success_mono': None, 'error': 'offline'},
        'unitree': {'value': {'episode_id': 'unitree_test', 'active': True,
            'state': 'recording', 'format_version': 2, 'ready': True,
            'quality_ok': True, 'progress_name': 'camera_records', 'progress_value': 120,
            'progress_unchanged_s': 0., 'message': 'episode=/home/unitree/heterovla-data/datasets/t/unitree_test\n'},
            'success_mono': 10., 'error': None}}


def test_stale_joint_manifest_does_not_steal_unitree_task_markers():
    ctx = capture_context(observations(), {'episode_id': 'old_joint', 'state': 'stop_failed'}, now=12.)
    assert ctx['key'] == 'unitree__unitree_test' and ctx['healthy']
    assert set(ctx['hosts']) == {'unitree'}
    assert ctx['hosts']['unitree']['directory'].endswith('/t/unitree_test')


@pytest.mark.parametrize('failure', ['not_ready', 'stalled', 'fault', 'no_progress', 'start_failed'])
def test_unconfirmed_or_failed_capture_cannot_advertise_healthy_marker_start(failure):
    records = observations()
    status = records['unitree']['value']
    manifest = None
    if failure == 'not_ready': status['ready'] = False
    if failure == 'stalled': status['progress_unchanged_s'] = 15.
    if failure == 'fault': status['fault'] = ['camera_write_error']
    if failure == 'no_progress': status['progress_value'] = 0
    if failure == 'start_failed': manifest = {'episode_id': 'unitree_test', 'state': 'start_failed'}
    assert not capture_context(records, manifest, now=12.)['healthy']


def test_lost_status_retains_bound_open_context_but_disables_start():
    prior = capture_context(observations(), None, now=12.)
    current = capture_context(observations(), None, previous=prior, now=40.)
    assert current['key'] == prior['key']
    assert not current['healthy']


def test_joint_scope_requires_both_actual_same_recordings():
    assert capture_context(observations(), None, 'joint', now=12.) is None
    records = observations()
    records['p450'] = copy.deepcopy(records['unitree'])
    records['p450']['value'].update(format_version=1,
        message=json.dumps({'recorder': {'session_dir': '/home/amov/p450_data/unitree_test'}}))
    ctx = capture_context(records, {'episode_id': 'unitree_test', 'state': 'starting'}, 'joint', now=12.)
    assert not ctx['healthy']
    ctx = capture_context(records, {'episode_id': 'unitree_test', 'state': 'recording', 't0_desktop_ns': 1}, 'joint', now=12.)
    assert ctx['healthy'] and set(ctx['hosts']) == {'unitree', 'p450'}


def test_auto_joint_task_remains_bound_when_one_host_drops_out():
    records = observations()
    records['p450'] = copy.deepcopy(records['unitree'])
    records['p450']['value'].update(format_version=1,
        message=json.dumps({'recorder': {'session_dir': '/home/amov/p450_data/unitree_test'}}))
    previous = capture_context(records, None, now=12.)
    assert previous['mode'] == 'joint'
    records['p450']['error'] = 'connection lost'
    result = capture_context(records, None, previous=previous, now=12.)
    assert result['key'] == previous['key'] and not result['healthy']
    assert set(result['hosts']) == {'unitree', 'p450'}


class NoClock:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): raise RuntimeError('test transport unavailable')
    def __exit__(self, *args): pass


def service(tmp_path, copied=None):
    ctx = capture_context(observations(), None, now=12.)
    return TaskMarkerService(tmp_path, {'unitree': 'unitree'}, lambda scope, prior: ctx,
        clock_factory=NoClock,
        copier=lambda host, destination, info, revision, bundle: (
            copied.append((host, info, revision, bundle)) if copied is not None else None)
            or info['directory']+'/annotations/task_markers')


def payload(action='start', revision=0, request='00000000-0000-4000-8000-000000000001'):
    return {'action': action, 'scope': 'auto', 'context_key': 'unitree__unitree_test',
            'request_id': request, 'expected_revision': revision, 'client_wall_ms': 1000}


def test_service_durably_records_before_background_copy_and_only_selected_host(tmp_path):
    copied = []
    srv = service(tmp_path, copied)
    try:
        result = srv.apply(payload(), wall_ns=1_000_000_000, monotonic_ns=10)
        assert result['current'] and result['revision'] == 1
        assert (tmp_path/'task_markers/unitree__unitree_test/events.jsonl').is_file()
        limit = time.monotonic()+2
        while not copied and time.monotonic() < limit: time.sleep(.01)
        assert len(copied) == 1 and copied[0][0] == 'unitree'
        assert 'task_segments.csv' in copied[0][3]
    finally: srv.close()


def test_delayed_request_cannot_mark_a_different_capture(tmp_path):
    srv = service(tmp_path)
    try:
        data = payload(); data['context_key'] = 'unitree__wrong_episode'
        with pytest.raises(RuntimeError, match='会话已变化'):
            srv.apply(data, wall_ns=1, monotonic_ns=1)
        assert not list((tmp_path/'task_markers').glob('*/events.jsonl'))
    finally: srv.close()


def test_replica_metadata_failure_does_not_strand_background_retry(tmp_path, monkeypatch):
    from jointctl.manifest import ManifestStore
    real = ManifestStore._atomic_json
    failed = []
    def write(path, value):
        if path.name == 'replica.json' and not failed:
            failed.append(True)
            raise PermissionError('temporary file lock')
        return real(path, value)
    monkeypatch.setattr(ManifestStore, '_atomic_json', write)
    copied = []
    srv = service(tmp_path, copied)
    try:
        result = srv.apply(payload(), wall_ns=1_000_000_000, monotonic_ns=10)
        assert result['current'] and result['sync']['state'] == 'failed'
        assert not srv.pending and srv.worker is None
        assert srv.snapshot()['revision'] == 1
        limit = time.monotonic()+2
        while not copied and time.monotonic() < limit: time.sleep(.01)
        assert len(copied) == 1
    finally: srv.close()


def test_marker_http_uses_origin_token_and_receipt_timestamp(tmp_path):
    from jointctl.console import make_server
    srv = service(tmp_path)
    class State:
        root = tmp_path
        config_path = tmp_path/'config.json'
        config = {}
        marker_snapshot = lambda self, scope: srv.snapshot(scope)
        mark_task = lambda self, data, **kwargs: srv.apply(data, **kwargs)
    server = make_server(State(), port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    def request(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse(); code = response.status; value = json.loads(response.read())
        conn.close(); return code, value
    try:
        assert request('GET', '/api/task-markers?scope=auto')[0] == 200
        assert request('POST', '/api/task-markers', json.dumps(payload()), {'Content-Type':'application/json'})[0] == 403
        token = request('GET', '/api/session')[1]['token']
        before = time.time_ns()
        code, result = request('POST', '/api/task-markers', json.dumps(payload()),
            {'Content-Type':'application/json', 'Origin':f'http://127.0.0.1:{server.server_port}',
             'X-Console-Token':token})
        assert code == 200 and before <= int(result['current']['start_desktop_ns']) <= time.time_ns()
        assert request('GET', '/api/task-markers?scope=wrong')[0] == 400
    finally:
        server.shutdown(); server.server_close(); worker.join(); srv.close()
