"""Bind task timestamps to real recordings and mirror small annotation sidecars."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import threading
import time
import uuid

from .clock_sync import ClockProbe
from .manifest import ManifestStore
from .task_markers import TaskMarkerStore
from .telemetry import safe_json

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\Z')


def recording_directory(host, status):
    """Get a directory from this status, never from an unrelated old manifest."""
    episode_id = status.get('episode_id')
    if not isinstance(episode_id, str) or not IDENTIFIER.fullmatch(episode_id):
        return None
    if host == 'unitree':
        for line in status.get('message', '').splitlines():
            if line.startswith('episode='):
                path = line.partition('=')[2].strip()
                if PurePosixPath(path).name == episode_id and path.startswith('/'):
                    return path
    else:
        from .remote import json_objects
        for obj in json_objects(status.get('message', '')):
            path = (obj.get('recorder') or {}).get('session_dir')
            if isinstance(path, str) and path.startswith('/') and PurePosixPath(path).name == episode_id:
                return path
    return None


def capture_context(records, episode, scope='auto', previous=None, *, now=0.):
    if scope not in {'auto', 'unitree', 'joint'}:
        raise ValueError('无效的打点采集范围')
    views = {}
    for host, record in records.items():
        status = copy.deepcopy(record.get('value') or {})
        age = None if record.get('success_mono') is None else max(0., now-record['success_mono'])
        budget = min(15., max(6., float(status.get('query_ms') or 0.) / 500. + 3.))
        status['fresh'] = age is not None and age <= budget and not record.get('error')
        views[host] = status
    live = {host: value for host, value in views.items()
            if value['fresh'] and value.get('active') is True and value.get('episode_id')}
    shared = (set(live) == {'p450', 'unitree'} and
              live['p450']['episode_id'] == live['unitree']['episode_id'])
    if (scope == 'auto' and previous and previous.get('mode') == 'joint' and not shared
            and (live.get('unitree') or {}).get('episode_id') == previous.get('episode_id')):
        return {**copy.deepcopy(previous), 'healthy': False,
                'reason': '联合采集的一端失联或已停止；保留原任务段，可结束并标记待核对'}
    chosen = ('joint' if shared else 'unitree') if scope == 'auto' else scope
    selected = ['p450', 'unitree'] if chosen == 'joint' else ['unitree']
    current_id = (live.get('unitree') or {}).get('episode_id')
    if chosen == 'joint' and not shared:
        current_id = None
    if current_id is None:
        if previous:
            return {**copy.deepcopy(previous), 'healthy': False,
                    'reason': '采集已停止或连接状态未确认；只能结束已打开的任务段'}
        return None
    key = chosen + '__' + current_id
    context = {'key': key, 'episode_id': current_id, 'mode': chosen,
               'healthy': True, 'reason': None, 'hosts': {}}
    for host in selected:
        value = views[host]
        directory = recording_directory(host, value)
        context['hosts'][host] = {'episode_id': value.get('episode_id'), 'directory': directory}
        problems = []
        if value.get('state') != 'recording' or not directory:
            problems.append('录制或数据路径尚未确认')
        if value.get('format_version') == 2 and value.get('ready') is not True:
            problems.append('采集尚未就绪')
        if value.get('quality_ok') is False or value.get('fault') or value.get('missing_states'):
            problems.append('采集报告异常')
        if not value.get('progress_name') or value.get('progress_value', 0) <= 0:
            problems.append('尚未确认数据写入')
        if value.get('progress_unchanged_s', 0) >= 15:
            problems.append('数据写入停滞')
        if problems:
            context['healthy'] = False
            context['reason'] = host + ': ' + '；'.join(problems)
    if episode and episode.get('episode_id') == current_id:
        if episode.get('state') != 'recording':
            context.update(healthy=False, reason='采集启动未成功确认，不能记录任务开始')
        elif chosen == 'joint' and not episode.get('t0_desktop_ns'):
            context.update(healthy=False, reason='联合采集共同开始时间尚未确认')
        context['capture_started_desktop_ns'] = episode.get('t0_desktop_ns')
    return context


REMOTE_WRITE = r'''
import base64, json, os, pathlib, uuid
p = json.load(__import__('sys').stdin)
root = pathlib.Path(p['root']).resolve()
d = pathlib.Path(p['directory']).resolve()
assert d != root and root in d.parents and d.name == p['episode_id'] and d.is_dir(), 'capture directory mismatch'
if p['host'] == 'unitree':
    assert json.loads((d/'meta.json').read_text())['episode_id'] == p['episode_id'], 'episode ownership mismatch'
out = d/'annotations'/'task_markers'
assert d in out.resolve().parents, 'annotation path escapes capture'
out.mkdir(parents=True, exist_ok=True)
allowed = {'events.jsonl','task_segments.json','task_segments.csv','task_clock_samples.jsonl'}
assert set(p['files']) <= allowed
for name, value in p['files'].items():
    data = base64.b64decode(value, validate=True)
    tmp = out/('.'+name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('xb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,out/name)
    assert (out/name).read_bytes() == data, 'annotation readback mismatch'
fd = os.open(str(out),os.O_RDONLY|os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
print(json.dumps({'directory':str(out),'revision':p['revision'],'files':list(p['files'])}))
'''


class TaskMarkerService:
    def __init__(self, root, destinations, context_provider, *, clock_factory=ClockProbe, copier=None):
        self.root = Path(root)
        self.store = TaskMarkerStore(self.root)
        self.destinations = destinations
        self.context_provider = context_provider
        self.clock_factory = clock_factory
        self.copier = copier or self._copy
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.pending = {}
        self.inflight = set()
        self.sync_states = {}
        self.previous = {}
        self.worker = None
        self.closed = False
        for path in (self.root / 'task_markers').glob('*/last_context.json'):
            try:
                context = json.loads(path.read_text(encoding='utf-8'))
                if IDENTIFIER.fullmatch(context['key']):
                    self.previous[context['mode']] = context
                    self.previous['auto'] = context
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _context(self, scope):
        if scope not in {'auto', 'unitree', 'joint'}:
            raise ValueError('无效的打点采集范围')
        context = self.context_provider(scope, self.previous.get(scope))
        if context:
            context = {**context, 'clock_session_id': self.session_id,
                       'timestamp_basis': 'desktop_receive_wall_ns'}
            self.previous[scope] = context
            self.previous[context['mode']] = context
        return context

    def snapshot(self, scope='auto'):
        with self.lock:
            context = self._context(scope)
            result = self.store.snapshot(context)
            if context:
                key = context['key']
                folder = self.root / 'task_markers' / key
                saved = self.sync_states.get(key)
                if saved is None:
                    try:
                        saved = json.loads((folder / 'replica.json').read_text(encoding='utf-8'))
                        saved['retry_at'] = 0.  # Previous process/boot monotonic epochs are not reusable.
                    except (OSError, ValueError):
                        saved = {'state': 'not_needed', 'revision': 0}
                result['sync'] = copy.deepcopy(saved)
                needs_sync = (saved.get('revision', 0) < result.get('revision', 0) or
                              saved.get('state') in {'pending', 'failed'})
                if result.get('revision', 0) and needs_sync and not result.get('error'):
                    if key not in self.pending and key not in self.inflight and time.monotonic() >= saved.get('retry_at', 0):
                        try:
                            self._enqueue(context, result)
                        except OSError as exc:
                            result['sync'] = {'state': 'failed', 'revision': result['revision'], 'error': str(exc)}
            return safe_json(result)

    def apply(self, payload, *, wall_ns, monotonic_ns):
        required = {'action', 'scope', 'context_key', 'request_id', 'expected_revision'}
        if not isinstance(payload, dict) or not required <= set(payload) or set(payload)-required-{'client_wall_ms'}:
            raise ValueError('打点请求字段无效')
        with self.lock:
            context = self._context(payload['scope'])
            if not context or payload['context_key'] != context['key']:
                raise RuntimeError('采集会话已变化，请刷新打点状态后重试')
            result = self.store.apply(payload['action'], context,
                request_id=payload['request_id'], expected_revision=payload['expected_revision'],
                wall_ns=wall_ns, monotonic_ns=monotonic_ns, client_wall_ms=payload.get('client_wall_ms'))
            folder = self.root / 'task_markers' / context['key']
            try:
                ManifestStore._atomic_json(folder / 'last_context.json', context)
                if result.get('error'):
                    raise OSError(result['error'])
                self._enqueue(context, result)
                result['sync'] = copy.deepcopy(self.sync_states[context['key']])
            except OSError as exc:
                result['sync'] = {'state': 'failed', 'revision': result['revision'], 'error': str(exc)}
            return safe_json(result)

    def _enqueue(self, context, snapshot):
        key, revision = context['key'], snapshot['revision']
        folder = self.root / 'task_markers' / key
        files = {name: (folder / name).read_bytes() for name in
                 ('events.jsonl', 'task_segments.json', 'task_segments.csv')}
        pending_state = {'state': 'pending', 'revision': revision, 'error': None}
        ManifestStore._atomic_json(folder / 'replica.json', pending_state)
        self.pending[key] = (copy.deepcopy(context), revision, files)
        self.sync_states[key] = pending_state
        if not self.worker or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._work, daemon=True)
            self.worker.start()
        self.condition.notify_all()

    def _work(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                if not self.pending:
                    self.condition.wait(timeout=1.)
                    continue
                key = next(iter(self.pending))
                context, revision, files = self.pending.pop(key)
                self.inflight.add(key)
            folder = self.root / 'task_markers' / key
            errors, clocks, directories = [], {}, {}
            for host, info in context['hosts'].items():
                destination = self.destinations[host]
                try:
                    with self.clock_factory(host, destination=destination, timeout_s=2.) as probe:
                        samples = [probe.sample().to_dict() for _ in range(5)]
                    best = min(samples, key=lambda sample: sample['rtt_ns'])
                    clocks[host] = {'estimated_error_ns': (best['rtt_ns']+1)//2,
                                    'offset_ns': best['offset_ns'], 'state': 'estimated'}
                    row = {'revision': revision, 'host': host, 'samples': samples,
                           'captured_at_desktop_ns': time.time_ns(), 'estimate': clocks[host]}
                    with (folder / 'task_clock_samples.jsonl').open('a', encoding='utf-8', newline='\n') as handle:
                        handle.write(json.dumps(row, ensure_ascii=False)+'\n')
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception as exc:
                    clocks[host] = {'state': 'unavailable', 'error': str(exc)}
                try:
                    bundle = dict(files)
                    clock_path = folder / 'task_clock_samples.jsonl'
                    if clock_path.exists():
                        bundle['task_clock_samples.jsonl'] = clock_path.read_bytes()
                    directories[host] = self.copier(host, destination, info, revision, bundle)
                except Exception as exc:
                    errors.append(host + ': ' + str(exc))
            sync = {'state': 'failed' if errors else 'synced', 'revision': revision,
                    'error': '; '.join(errors) or None, 'remote_directories': directories,
                    'clock': clocks, 'retry_at': time.monotonic()+10.}
            with self.lock:
                self.inflight.discard(key)
                if self.sync_states.get(key, {}).get('revision') == revision:
                    self.sync_states[key] = sync
                    ManifestStore._atomic_json(folder / 'replica.json', sync)

    @staticmethod
    def _copy(host, destination, info, revision, files):
        body = {'host': host, 'root': '/home/unitree/heterovla-data/datasets' if host == 'unitree' else '/home/amov/p450_data',
                'directory': info['directory'], 'episode_id': info['episode_id'], 'revision': revision,
                'files': {name: base64.b64encode(data).decode('ascii') for name, data in files.items()}}
        result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            destination, 'python3 -c ' + shlex.quote(REMOTE_WRITE)], input=json.dumps(body),
            capture_output=True, encoding='utf-8', errors='replace', timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or '打点文件同步失败')
        return json.loads(result.stdout)['directory']

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=2.)
