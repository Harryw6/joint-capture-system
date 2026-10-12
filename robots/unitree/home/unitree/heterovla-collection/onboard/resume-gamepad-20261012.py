"""One-time process restart adapter: append the selected CSV, never overwrite it."""
import csv
from pathlib import Path
import runpy
import sys

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target.parent))
import session_support


class ResumeSegmentCsv(session_support.SegmentCsv):
    def __init__(self, selector, data_root, fields):
        super().__init__(selector, data_root, fields)
        selected = session_support.read_json(self.selector, {}).get('episode')
        if selected:
            path = session_support.episode_path(selected, data_root) / 'raw/piper_gamepad.csv'
            with path.open(newline='') as previous:
                if next(csv.reader(previous), None) != list(fields):
                    raise RuntimeError('existing gamepad CSV header mismatch; refusing to append')
            self.handle = path.open('a', newline='')
            self.writer = csv.DictWriter(self.handle, fieldnames=fields)
            self.episode = selected


session_support.SegmentCsv = ResumeSegmentCsv
sys.argv = sys.argv[1:]
runpy.run_path(str(target), run_name='__main__')
