"""Exercise annotation transport in robot /tmp, without marking a real capture."""
import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from jointctl.clock_sync import ClockProbe
from jointctl.task_marker_service import REMOTE_WRITE
from jointctl.task_markers import TaskMarkerStore


def ssh(command, body=None):
    result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
        'unitree', command], input=body, capture_output=True, text=True, encoding='utf-8',
        timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


setup = """import tempfile,pathlib,json
root=pathlib.Path(tempfile.mkdtemp(prefix='joint-task-marker-smoke-',dir='/tmp'))
d=root/'marker_test'; d.mkdir()
(d/'meta.json').write_text(json.dumps({'episode_id':'marker_test','test_only':True}))
print(json.dumps({'root':str(root),'directory':str(d)}))
"""
fixture = ssh('python3 -c '+shlex.quote(setup))
with tempfile.TemporaryDirectory(prefix='task-marker-transport-') as temporary:
    store = TaskMarkerStore(temporary)
    context = {'key': 'unitree__marker_test', 'episode_id': 'marker_test', 'mode': 'unitree',
        'healthy': True, 'clock_session_id': 'smoke', 'hosts': {'unitree': {
            'episode_id': 'marker_test', 'directory': fixture['directory']}}}
    for index, action in enumerate(('start', 'reset', 'stop', 'start', 'stop')):
        store.apply(action, context, request_id=str(uuid.uuid4()), expected_revision=index,
            wall_ns=1_791_784_800_000_000_000+index*1_000_000_000,
            monotonic_ns=10_000_000_000+index*1_000_000_000)
    folder = Path(store.snapshot(context)['local_directory'])
    names = ('events.jsonl', 'task_segments.json', 'task_segments.csv')
    body = {**fixture, 'host': 'unitree', 'episode_id': 'marker_test', 'revision': 5,
            'files': {name: base64.b64encode((folder/name).read_bytes()).decode('ascii') for name in names}}
    copied = ssh('python3 -c '+shlex.quote(REMOTE_WRITE), json.dumps(body))
    assert copied['revision'] == 5 and set(copied['files']) == set(names)
    with ClockProbe('unitree', timeout_s=2.) as probe:
        samples = [probe.sample() for _ in range(5)]
    best = min(samples, key=lambda sample: sample.rtt_ns)
    print(json.dumps({'transport': 'verified_by_remote_readback', 'task_pairs': 2,
        'reset_history': 1, 'remote_test_directory': copied['directory'],
        'clock_best_rtt_ms': best.rtt_ns/1e6, 'clock_error_estimate_ms': best.rtt_ns/2e6}))

# Remove only the exact temporary fixture created by this script.
cleanup = """import json,pathlib,shutil,sys
p=json.load(sys.stdin); root=pathlib.Path(p['root'])
assert root.is_absolute() and root.parent==pathlib.Path('/tmp') and root.name.startswith('joint-task-marker-smoke-')
assert not root.is_symlink() and (root/'marker_test/meta.json').is_file()
assert json.loads((root/'marker_test/meta.json').read_text()).get('test_only') is True
shutil.rmtree(root)
print(json.dumps({'temporary_fixture_removed':True}))
"""
ssh('python3 -c '+shlex.quote(cleanup), json.dumps(fixture))
