"""Install exactly three reviewed modules on an idle Unitree host, with rollback.

Run with --stage DIR to check; add --apply to install. Never starts a controller.
The stale recording pointer, recordings, SDK, CAN setup and services are untouched.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil


BASELINE = {
    'safe_teleop.py': '4d402d3249b132533b0cdaa1506fb972d3a394afcd58609c8f6fc742029da925',
    'piper_gamepad_teleop.py': '3088a03ebbc249c6c9b2437111dced52e6395d0f40993abe1c6c50158036c1ee',
    'piper_safety.py': None,
}
BLOCKED = {'piper_gamepad_teleop.py', 'raw_capture.py', 'camera_session.py',
           'hetero_pkl_recorder.py', 'piper_stream_loop.py', 'piper_chunk_loop.py',
           'hetero_teleop_loop.py', 'arm_hold_daemon.py', 'collection_manager.py'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path('/home/unitree/heterovla-collection').resolve(strict=True)
    target = (root / 'onboard').resolve(strict=True)
    if target.parent != root:
        raise RuntimeError('Unexpected onboard path')
    stage = args.stage.resolve(strict=True)
    manifest = json.loads((stage / 'release.json').read_text())
    payload = {}
    for name in BASELINE:
        data = (stage / name).read_bytes()
        if digest(data) != manifest['sha256'][name]:
            raise RuntimeError('Staged checksum mismatch: ' + name)
        compile(data, name, 'exec')
        payload[name] = data

    with (root / 'run/operation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for proc in Path('/proc').glob('[0-9]*/cmdline'):
            if int(proc.parent.name) == os.getpid():
                continue
            try:
                argv = [a.decode(errors='replace') for a in proc.read_bytes().split(b'\0') if a]
            except FileNotFoundError:
                continue
            if any(Path(arg).name in BLOCKED or 'Gamepad_PiPER/' in arg for arg in argv):
                raise RuntimeError('Capture/control process active: ' + proc.parent.name)
        previous = {}
        for name, expected in BASELINE.items():
            path = target / name
            if path.is_symlink():
                raise RuntimeError('Refusing symlink: ' + name)
            data = path.read_bytes() if path.exists() else None
            if (digest(data) if data is not None else None) != expected:
                raise RuntimeError('Production version changed; review before install: ' + name)
            previous[name] = data
        report = {'checked': True, 'applied': False, 'sha256': manifest['sha256'],
                  'recording_pointer_retained': (root / 'run/active_episode').exists(),
                  'started_processes': False, 'sent_robot_commands': False}
        if args.apply:
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup = root / 'backups' / ('piper-safety-' + stamp)
            backup.mkdir(parents=True, exist_ok=False)
            for name, data in previous.items():
                if data is not None:
                    shutil.copy2(target / name, backup / name)
            (backup / 'before.json').write_text(json.dumps(BASELINE, indent=2) + '\n')
            installed = []
            try:
                for name, data in payload.items():
                    path = target / name
                    temporary = target / (name + '.installing')
                    with temporary.open('wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
                    os.replace(temporary, path)
                    installed.append(name)
                    if digest(path.read_bytes()) != manifest['sha256'][name]:
                        raise RuntimeError('Installed checksum mismatch: ' + name)
            except BaseException:
                for name in reversed(installed):
                    if previous[name] is None:
                        (target / name).unlink()
                    else:
                        shutil.copy2(backup / name, target / name)
                raise
            report.update(applied=True, backup=str(backup))
            (backup / 'installed.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
