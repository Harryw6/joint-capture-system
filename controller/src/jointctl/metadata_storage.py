"""Durable Unitree storage for desktop-measured clock evidence.

The desktop remains the clock reference and keeps a retry cache. Uploads run
outside probe threads so disk/network latency cannot alter probe timestamps.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
import uuid


DEFAULT_ROOT = "/home/unitree/heterovla-data/joint_manifests"
CLOCK_FILES = {"clock_p450.jsonl", "clock_unitree.jsonl"}
JSON_FILES = {"manifest.json", "clock_monitor_config.json", "raw_health.json", "alignment.json"}
FILES = CLOCK_FILES | JSON_FILES

# Runs with the host's standard Python, without installing a robot-side daemon.
REMOTE_SCRIPT = r'''
import base64, fcntl, hashlib, json, os, pathlib, re, sys, tempfile
request = json.load(sys.stdin)
root = pathlib.Path(request['root'])
episode = request['episode']
assert root.is_absolute() and '..' not in root.parts
assert re.fullmatch(r'[A-Za-z0-9_-]+', episode)
allowed = {'manifest.json','clock_p450.jsonl','clock_unitree.jsonl',
           'clock_monitor_config.json','raw_health.json','alignment.json'}
folder = root / episode
assert not folder.is_symlink()
if request['action'] == 'read':
    assert folder.is_dir(), 'metadata episode not found'
else:
    root.mkdir(parents=True, exist_ok=True)
    folder.mkdir(exist_ok=True)
def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
sync_dir(root)
lock = folder / '.storage.lock'
assert not lock.is_symlink()
with lock.open('a+b') as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    if request['action'] == 'read':
        result = {}
        for name in sorted(allowed):
            path = folder / name
            assert not path.is_symlink()
            if path.is_file():
                data = path.read_bytes()
                result[name] = {'data':base64.b64encode(data).decode(),
                                'sha256':hashlib.sha256(data).hexdigest()}
        assert 'manifest.json' in result
        print(json.dumps({'files':result}))
    else:
        result = {}
        for item in request['files']:
            name = item['name']; assert name in allowed
            path = folder / name; assert not path.is_symlink()
            data = base64.b64decode(item['data'], validate=True)
            assert hashlib.sha256(data).hexdigest() == item['sha256']
            if name.endswith('.jsonl'):
                assert not data or data.endswith(b'\n')
                for line in data.splitlines(): assert isinstance(json.loads(line), dict)
                offset = item['offset']; assert isinstance(offset,int) and offset >= 0
                with path.open('a+b') as output:
                    output.seek(0,2); size = output.tell()
                    assert size >= offset, 'remote clock prefix is missing'
                    overlap = min(len(data), size-offset)
                    output.seek(offset)
                    assert output.read(overlap) == data[:overlap], 'remote clock evidence differs'
                    output.seek(0,2)
                    output.write(data[overlap:]); output.flush(); os.fsync(output.fileno())
                    if request.get('final'): assert output.tell() == offset+len(data)
                    output.seek(offset)
                    assert hashlib.sha256(output.read(len(data))).hexdigest() == item['sha256']
                result[name] = {'offset':offset+len(data),'sha256':item['sha256']}
            else:
                value = json.loads(data); assert isinstance(value,dict)
                if name == 'manifest.json':
                    assert value['episode_id'] == episode
                    if path.exists():
                        prior = json.loads(path.read_bytes())
                        assert all(prior.get(k) == value.get(k) for k in
                                   ('episode_id','created_desktop_ns','label','mode')), 'episode identity conflict'
                fd, temporary = tempfile.mkstemp(prefix='.'+name+'.', dir=folder)
                try:
                    with os.fdopen(fd,'wb') as output:
                        output.write(data); output.flush(); os.fsync(output.fileno())
                    os.replace(temporary,path)
                    assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
                result[name] = {'sha256':item['sha256']}
        sync_dir(folder)
        print(json.dumps({'files':result}))
'''


class MetadataStorageError(RuntimeError):
    def __init__(self, message: str, failure_kind: str = "remote_command"):
        super().__init__(message)
        self.failure_kind = failure_kind


class MetadataStorage:
    def __init__(self, host: str, root: str = DEFAULT_ROOT, *, interval_s: float = 2.0,
                 timeout_s: float = 5.0, runner=None):
        if not host or host.startswith('-') or any(c.isspace() for c in host):
            raise ValueError("invalid metadata storage SSH host")
        path = PurePosixPath(root)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError("metadata storage root must be an absolute robot path")
        if not all(math.isfinite(v) and v > 0 for v in (interval_s, timeout_s)):
            raise ValueError("metadata storage interval and timeout must be positive")
        self.host, self.root = host, str(path)
        self.interval_s, self.timeout_s = interval_s, timeout_s
        self.runner = runner or subprocess.run
        self.offsets: dict[tuple[str, str], int] = {}

    @classmethod
    def from_config(cls, config):
        settings = config.get('metadata_storage')
        if settings is None:
            return None  # Older configurations retain their existing behavior.
        if not isinstance(settings, dict):
            raise ValueError("metadata_storage must be an object")
        return cls(str(config.get('unitree', {}).get('host', 'unitree')),
                   str(settings.get('root', DEFAULT_ROOT)),
                   interval_s=float(settings.get('interval_s', 2)),
                   timeout_s=float(settings.get('timeout_s', 5)))

    def settings(self):
        return {'host': self.host, 'root': self.root, 'interval_s': self.interval_s,
                'timeout_s': self.timeout_s}

    def remote_path(self, episode_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', episode_id):
            raise ValueError("invalid metadata episode ID")
        return str(PurePosixPath(self.root) / episode_id)

    def _request(self, episode_id, **payload):
        self.remote_path(episode_id)
        code = base64.b64encode(REMOTE_SCRIPT.encode()).decode('ascii')
        command = "python3 -c \"exec(__import__('base64').b64decode('" + code + "'))\""
        try:
            result = self.runner(['ssh', '-T', '-o', 'BatchMode=yes', '-o',
                                  'ConnectTimeout=5', self.host, command],
                                 input=json.dumps({'root':self.root, 'episode':episode_id, **payload}),
                                 capture_output=True, text=True, encoding='utf-8',
                                 timeout=self.timeout_s, check=False,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MetadataStorageError(f"Unitree metadata storage unavailable: {exc}", 'connectivity') from exc
        if result.returncode:
            raise MetadataStorageError(f"Unitree metadata write/read failed: {result.stderr[-2000:]}",
                                       'connectivity' if result.returncode == 255 else 'remote_command')
        try:
            reply = json.loads(result.stdout)['files']
            if not isinstance(reply, dict) or not all(isinstance(v, dict) for v in reply.values()):
                raise ValueError('invalid files acknowledgement')
            return reply
        except (ValueError, KeyError, TypeError) as exc:
            raise MetadataStorageError("invalid Unitree metadata acknowledgement") from exc

    @staticmethod
    def _status(folder, value):
        path = Path(folder) / 'metadata_storage_status.json'
        tmp = path.with_name('.ms-' + uuid.uuid4().hex[:16] + '.tmp')
        try:
            tmp.write_text(json.dumps(value, ensure_ascii=False) + '\n', encoding='utf-8')
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def sync(self, folder: Path, *, final=False):
        folder = Path(folder)
        episode = folder.name
        items = []
        try:
            for name in sorted(FILES):
                path = folder / name
                if not path.is_file():
                    continue
                offset = self.offsets.get((episode, name), 0) if name in CLOCK_FILES and not final else 0
                with path.open('rb') as source:
                    source.seek(0, 2)
                    if source.tell() < offset:
                        raise MetadataStorageError("local clock evidence was truncated")
                    source.seek(offset); data = source.read()
                if name in CLOCK_FILES:
                    if final and data and not data.endswith(b'\n'):
                        raise MetadataStorageError("incomplete local clock record")
                    data = data[:data.rfind(b'\n')+1] if data else data
                items.append({'name':name,'offset':offset,'data':base64.b64encode(data).decode(),
                              'sha256':hashlib.sha256(data).hexdigest()})
            if not any(item['name']=='manifest.json' for item in items):
                raise MetadataStorageError("owning manifest is missing")
            if final and not CLOCK_FILES.issubset({item['name'] for item in items}):
                raise MetadataStorageError("both clock evidence files are required for final storage")
            reply = self._request(episode, action='write', files=items, final=final)
            for item in items:
                ack = reply.get(item['name'], {})
                if ack.get('sha256') != item['sha256']:
                    raise MetadataStorageError("Unitree metadata checksum acknowledgement differs")
                if item['name'] in CLOCK_FILES:
                    end = item['offset'] + len(base64.b64decode(item['data']))
                    if ack.get('offset') != end:
                        raise MetadataStorageError("Unitree metadata byte acknowledgement differs")
                    self.offsets[(episode, item['name'])] = end
            self._status(folder, {'saved':True,'final':final,'at_wall_ns':time.time_ns(),
                                  'host':self.host,'path':self.remote_path(episode), 'error':None})
        except Exception as exc:
            try:
                self._status(folder, {'saved':False,'final':False,'at_wall_ns':time.time_ns(),
                                      'host':self.host,'path':self.remote_path(episode),'error':str(exc)})
            except OSError as status_error:
                print(f'本地保存状态写入失败：{status_error}', file=sys.stderr, flush=True)
            raise

    def run(self, folder: Path, stop: threading.Event):
        while True:
            try:
                self.sync(folder)
            except Exception as exc:
                print(f'对时文件保存到机器狗失败，将从本地缓存重试：{exc}', file=sys.stderr, flush=True)
            if stop.wait(self.interval_s):
                return

    def health_reason(self, folder: Path, now_ns: int) -> str | None:
        try:
            saved = json.loads((Path(folder) / 'metadata_storage_status.json').read_text(encoding='utf-8'))
            age = (now_ns - saved['at_wall_ns']) / 1e9
            if (saved.get('saved') and age >= 0 and
                    age <= self.interval_s + self.timeout_s + 2):
                return None
            return '对时文件保存到机器狗尚未确认：' + str(saved.get('error') or '保存状态过期')
        except (OSError, ValueError, KeyError, TypeError):
            return '对时文件保存到机器狗尚未确认'

    def fetch(self, folder: Path):
        """Fetch a stopped episode without replacing conflicting local evidence."""
        folder = Path(folder)
        reply = self._request(folder.name, action='read')
        decoded = {}
        for name, item in reply.items():
            if name not in FILES:
                raise MetadataStorageError('unexpected remote metadata filename')
            data = base64.b64decode(item['data'], validate=True)
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise MetadataStorageError('downloaded metadata checksum differs')
            decoded[name] = data
        manifest = json.loads(decoded['manifest.json'])
        if manifest['episode_id'] != folder.name:
            raise MetadataStorageError('downloaded episode ID differs')
        if manifest['state'] not in {'complete','start_failed','partial','stop_failed'}:
            raise MetadataStorageError('stop the capture before fetching metadata')
        for name, data in decoded.items():
            path = folder / name
            if path.exists() and path.read_bytes() != data:
                raise MetadataStorageError(f'local metadata differs: {name}; use a new cache directory')
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in decoded.items():
            path = folder / name
            if not path.exists():
                temporary = path.with_name('.fetch-' + uuid.uuid4().hex[:16] + '.tmp')
                try:
                    with temporary.open('xb') as output:
                        output.write(data); output.flush(); os.fsync(output.fileno())
                    os.link(temporary, path)  # Atomic publication without replacing local evidence.
                finally:
                    temporary.unlink(missing_ok=True)
        return manifest
