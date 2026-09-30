"""No CAN, joystick, GPU or robot imports: exercise real safety code and input methods."""
import ast
import logging
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'remote/unitree'))
from safe_teleop import safe_controller
from piper_safety import SafetyGuard, FEEDBACK_IDS, ENABLE_IDS, monitored_piper
from piper_gamepad_teleop import send_command


class SDK:
    def __init__(self):
        self.logger = logging.getLogger('test-piper-' + str(id(self)))
        self.logger.setLevel(logging.DEBUG)
        self.calls = []
        self.q = np.radians([10., 20., -30., 5., 4., 3.])
        self.width = .03
        self.missing = set()
        self.enabled = [True] * 6
        self.status = SimpleNamespace(arm_status=0, err_code=0)
        self.fail = None
        self.fail_kind = 'log'

    def feedback_fresh(self, ids, timeout):
        return not self.missing.intersection(ids)

    def GetArmJointMsgs(self):
        return SimpleNamespace(joint_state=SimpleNamespace(**{
            'joint_' + str(i + 1): float(v) for i, v in enumerate(np.degrees(self.q) * 1000)}))

    def GetArmGripperMsgs(self):
        return SimpleNamespace(gripper_state=SimpleNamespace(grippers_angle=self.width * 1e6))

    def GetArmStatus(self): return SimpleNamespace(arm_status=self.status)
    def GetArmEnableStatus(self): return self.enabled

    def __getattr__(self, name):
        assert name in ('ConnectPort', 'ModeCtrl', 'JointCtrl', 'GripperCtrl', 'EnableArm', 'MotionCtrl_1')
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.fail == name:
                if self.fail_kind == 'log': self.logger.error('0x151 send failed: SendCanMessage(SEND_MESSAGE_FAILED)')
                elif self.fail_kind == 'exception': raise OSError('CAN down')
                else: return False
        return call


class Base:
    def __init__(self, sdk):
        self.interface = sdk
        self.joint_angles = np.zeros(6)
        self.joint_limits = [(-math.pi, math.pi)] * 6
        self.xyz_rpy = np.zeros(6)
        self.gripper_state = 0.
        self.gripper_max_width = .07
        self.deadzone = .1
        self.arm_connected = self.arm_enabled = False
        self.joystick_connected = True
        self.axes = dict.fromkeys(('left_x', 'left_y', 'right_x', 'right_y', 'left_trigger', 'right_trigger'), 0.)
        self.hat = (0, 0)
        self.pressed = set()
        self.button_map = dict(zip(('home', 'b', 'y', 'back'), range(4)))
        self.buttons = {n: Edge() for n in self.button_map}
        self.joystick = SimpleNamespace(get_numbuttons=lambda: 4,
            get_button=lambda i: int(i in self.pressed), get_name=lambda: 'mock')
    def _handle_joystick_events(self): return self.joystick_connected
    def _get_axis_value(self, name): return self.axes[name]
    def _get_hat_value(self, name): return self.hat
    def _check_button_long_press(self): pass
    def _joint_to_pose(self): pass


class Edge:
    def __init__(self): self.last = False
    def update(self, value):
        event = value and not self.last
        self.last = value
        return event


# Use the upstream deadzone/state implementation, without executing its imports/init.
upstream = ROOT.parent / 'robots/unitree/home/unitree/Gamepad_PiPER_runtime/Gamepad_PiPER/src/gamepad_base.py'
tree = ast.parse(upstream.read_text(encoding='utf-8'))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'GamepadBase')
for node in cls.body:
    if isinstance(node, ast.FunctionDef) and node.name in ('_apply_deadzone', 'get_state'):
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(upstream), 'exec'), ns)
        setattr(Base, node.name, ns[node.name])


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr('safe_teleop.time.monotonic', lambda: clock.now)
    sdk = SDK()
    guard = SafetyGuard(sdk, clock=lambda: clock.now)
    ctl = safe_controller(Base)(sdk)
    ctl.attach_safety(guard)
    def tick(dt=.005):
        clock.now += dt
        ctl.update()
        return send_command(sdk, ctl, ctl.get_state(), gamepad_connected=ctl.joystick_connected)
    def arm():
        ctl.pressed = {0}
        tick()
        ctl.pressed = set()
        tick()
        assert not ctl.command_inhibited
    yield SimpleNamespace(ctl=ctl, sdk=sdk, guard=guard, clock=clock, tick=tick, arm=arm)
    guard.logger.removeHandler(guard.errors)


def test_initialization_never_enables_or_sends_motion(rig):
    rig.tick()
    assert rig.ctl.command_inhibited and rig.sdk.calls == []


def test_home_starts_from_feedback_without_zero_pose_or_reset(rig):
    rig.arm()
    np.testing.assert_allclose(rig.ctl.joint_angles, rig.sdk.q)
    assert all(args != (0, 0, 0, 0, 0, 0) for n, args, _ in rig.sdk.calls if n == 'JointCtrl')
    assert not any(n == 'MotionCtrl_1' for n, _, _ in rig.sdk.calls)
    names = [n for n, _, _ in rig.sdk.calls]
    assert names.index('JointCtrl') < names.index('EnableArm')


@pytest.mark.parametrize('axis,hat,joint', [('left_x',(0,0),0), ('left_y',(0,0),1),
    ('right_y',(0,0),2), (None,(1,0),3), (None,(0,1),4), ('right_x',(0,0),5)])
def test_hold_for_five_seconds_is_bounded_then_release_cancels_target(rig, axis, hat, joint):
    rig.arm()
    if axis: rig.ctl.axes[axis] = 1.
    rig.ctl.hat = hat
    for _ in range(1000): assert rig.tick()
    assert abs(rig.ctl.joint_angles[joint] - rig.sdk.q[joint]) <= math.radians(2.) + 1e-8
    rig.ctl.axes = dict.fromkeys(rig.ctl.axes, 0.)
    rig.ctl.hat = (0, 0)
    assert rig.tick()
    np.testing.assert_allclose(rig.ctl.joint_angles, rig.sdk.q)
    # Holding stays at the release position; it does not continually follow drift.
    held = rig.ctl.joint_angles.copy()
    rig.sdk.q[joint] += math.radians(.1)
    assert rig.tick()
    np.testing.assert_allclose(rig.ctl.joint_angles, held)


def test_reversal_discards_old_forward_target(rig):
    rig.arm()
    rig.ctl.axes['left_x'] = 1.
    for _ in range(60): rig.tick()
    rig.ctl.axes['left_x'] = -1.
    rig.tick()
    assert rig.ctl.joint_angles[0] > rig.sdk.q[0]
    assert rig.ctl.joint_angles[0] - rig.sdk.q[0] < math.radians(.1)


def test_gripper_hold_release_and_reverse_are_bounded(rig):
    rig.arm()
    rig.ctl.axes['right_trigger'] = 1.
    for _ in range(1000): assert rig.tick()
    assert rig.ctl.gripper_state / 100 * .07 <= rig.sdk.width + .001 + 1e-10
    rig.ctl.axes['right_trigger'] = 0.
    rig.tick()
    assert rig.ctl.gripper_state / 100 * .07 == pytest.approx(rig.sdk.width)


def test_releasing_one_axis_holds_it_while_other_axis_remains_active(rig):
    rig.arm()
    rig.ctl.axes.update(left_x=1., right_y=1.)
    for _ in range(50): rig.tick()
    rig.ctl.axes['left_x'] = 0.
    assert rig.tick()
    assert rig.ctl.joint_angles[0] == pytest.approx(rig.sdk.q[0])
    assert rig.ctl.joint_angles[2] > rig.sdk.q[2]


def test_delayed_iteration_does_not_catch_up_all_missed_motion(rig):
    rig.arm()
    rig.ctl.axes['left_x'] = 1.
    assert rig.tick(.1)
    assert abs(rig.ctl.joint_angles[0] - rig.sdk.q[0]) <= math.radians(.2) + 1e-8


@pytest.mark.parametrize('value', [float('nan'), 10000.])
def test_invalid_joint_feedback_blocks_motion(rig, value):
    rig.arm()
    rig.sdk.q[0] = value
    assert not rig.tick() and rig.guard.stop_requested


def test_target_outside_joint_limits_cannot_reach_sender(rig):
    rig.arm()
    rig.ctl.joint_limits[0] = (0., rig.sdk.q[0])
    rig.ctl.joint_angles[0] = rig.sdk.q[0] + math.radians(.1)
    assert not rig.ctl.send_safe_command()
    assert rig.guard.stop_requested


def test_inactive_shutdown_does_not_send_any_robot_command(rig):
    rig.guard.close()
    assert rig.sdk.calls == []


@pytest.mark.parametrize('axis', ['left_x', 'left_trigger'])
def test_home_rejects_non_neutral_inputs(rig, axis):
    rig.ctl.axes[axis] = 1.
    rig.ctl.pressed = {0}
    rig.tick()
    assert rig.ctl.command_inhibited and rig.sdk.calls == []


def test_disconnect_stops_and_reconnect_does_not_rearm(rig):
    rig.arm()
    rig.ctl.joystick_connected = False
    assert not rig.tick()
    assert ('MotionCtrl_1', (1, 0, 0), {}) in rig.sdk.calls
    rig.ctl.joystick_connected = True
    assert not rig.tick()
    assert rig.guard.inhibited


@pytest.mark.parametrize('missing', FEEDBACK_IDS + ENABLE_IDS)
def test_each_stale_feedback_stream_stops(rig, missing):
    rig.arm()
    rig.sdk.missing.add(missing)
    assert not rig.tick()
    assert rig.guard.inhibited and rig.guard.stop_requested


@pytest.mark.parametrize('failure', ['log', 'exception', 'false'])
def test_send_failure_blocks_remaining_commands_and_requests_stop(rig, failure):
    rig.arm()
    rig.sdk.calls.clear()
    rig.sdk.fail, rig.sdk.fail_kind = 'ModeCtrl', failure
    assert not rig.tick()
    assert not any(n in ('JointCtrl', 'GripperCtrl') for n, _, _ in rig.sdk.calls)
    assert rig.guard.stop_requested


def test_watchdog_stops_without_a_new_control_tick(rig):
    rig.arm()
    rig.clock.now += .3
    rig.guard.poll()
    assert rig.guard.inhibited and rig.guard.stop_requested
    assert not rig.ctl.send_safe_command()


def test_real_watchdog_thread_stops_a_stalled_loop():
    sdk = SDK()
    guard = SafetyGuard(sdk)
    guard.loop_timeout = .04
    guard.active = True
    guard.acknowledge()
    guard.start()
    try:
        deadline = time.monotonic() + 1.
        while not guard.stop_requested and time.monotonic() < deadline:
            time.sleep(.01)
        assert guard.stop_requested
    finally:
        guard.close()
    assert not guard.thread.is_alive()


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), 1.5])
def test_invalid_input_stops(rig, bad):
    rig.arm()
    rig.ctl.axes['left_x'] = bad
    assert not rig.tick() and rig.guard.stop_requested


def test_home_while_active_only_requests_stop(rig):
    rig.arm()
    rig.sdk.calls.clear()
    rig.ctl.pressed = {0}
    rig.tick()
    assert [n for n, _, _ in rig.sdk.calls] == ['MotionCtrl_1']
    assert rig.sdk.calls[0][1] == (1, 0, 0)


def test_fault_requires_neutral_home_and_healthy_feedback(rig):
    rig.arm()
    rig.ctl.safety_stop('fault')
    rig.sdk.status.arm_status = 1
    rig.ctl.pressed = {0}
    assert not rig.tick()
    assert not any(n == 'MotionCtrl_1' and args[0] == 2 for n, args, _ in rig.sdk.calls)
    rig.ctl.pressed = set()
    rig.tick()
    rig.sdk.status.arm_status = 0  # Operator reset, not performed by the teleop code.
    rig.arm()
    np.testing.assert_allclose(rig.ctl.joint_angles, rig.sdk.q)


def test_enable_is_nonblocking_and_times_out(rig):
    rig.sdk.enabled = [False] * 6
    rig.ctl.pressed = {0}
    rig.tick()
    rig.ctl.pressed = set()
    for _ in range(610): rig.tick()
    assert rig.guard.inhibited and rig.guard.stop_requested
    assert rig.ctl._arm_deadline is None


def test_fault_during_enabling_cannot_be_automatically_acknowledged(rig):
    rig.sdk.enabled = [False] * 6
    rig.ctl.pressed = {0}
    rig.tick()
    rig.ctl.pressed = set()
    rig.guard.trip('watchdog fault')
    rig.sdk.enabled = [True] * 6
    assert not rig.tick()
    assert rig.guard.inhibited and rig.ctl._arm_deadline is None


def test_shortcuts_cannot_home_replay_or_change_mode(rig):
    rig.arm()
    rig.ctl._handle_button_events({'y': True, 'b': True, 'back': True})
    rig.ctl._toggle_up_level_mode()
    rig.ctl._toggle_low_level_mode()
    rig.ctl._toggle_command_mode()
    rig.ctl._pose_to_joint(None, None)
    assert rig.ctl.command_mode == 0
    assert rig.ctl.up_level_mode == rig.ctl.low_level_mode == 'joint'
    with pytest.raises(ValueError): safe_controller(Base, joint_only=False)


def test_stop_retries_are_bounded_and_never_report_unconfirmed_success(rig):
    rig.arm()
    rig.sdk.fail = 'MotionCtrl_1'
    rig.ctl.safety_stop('fault')
    for _ in range(100):
        rig.clock.now += .1
        rig.guard.poll()
    assert sum(n == 'MotionCtrl_1' for n, _, _ in rig.sdk.calls) == 3
    assert rig.guard.stop_error and not rig.guard.stop_confirmed


def test_sdk_aggregate_freshness_cannot_hide_missing_joint_pair(monkeypatch):
    clock = [10.]
    monkeypatch.setattr('piper_safety.time.monotonic', lambda: clock[0])
    class Parser:
        def ParseCANFrame(self, frame): pass
    sdk = monitored_piper(Parser)()
    def frame(key): return SimpleNamespace(arbitration_id=key, data=bytes(8),
        is_error_frame=False, is_remote_frame=False, is_extended_id=False)
    for key in FEEDBACK_IDS: sdk.ParseCANFrame(frame(key))
    assert sdk.feedback_fresh(FEEDBACK_IDS, .25)
    clock[0] += .3
    for key in FEEDBACK_IDS:
        if key != 0x2A7: sdk.ParseCANFrame(frame(key))
    assert not sdk.feedback_fresh(FEEDBACK_IDS, .25)


@pytest.mark.parametrize('failure', ['input', 'disk', 'signal'])
def test_runner_stops_on_input_exception_disk_failure_and_sigterm(rig, monkeypatch, tmp_path, failure):
    import piper_gamepad_teleop as runner
    handlers, quit_calls = {}, []
    monkeypatch.setitem(sys.modules, 'piper_sdk', SimpleNamespace(C_PiperInterface_V2=object))
    monkeypatch.setitem(sys.modules, 'pygame', SimpleNamespace(quit=lambda: quit_calls.append(True)))
    monkeypatch.setattr(runner, 'monitored_piper', lambda _: lambda *a, **k: rig.sdk)
    monkeypatch.setattr(runner, 'load_controller', lambda _: lambda *a, **k: rig.ctl)
    monkeypatch.setattr(runner.signal, 'signal', lambda sig, handler: handlers.update({sig: handler}))
    rig.ctl.pressed = {0}
    original_snapshot = runner.input_snapshot
    after_signal = []
    def snapshot(controller):
        if failure == 'input': raise RuntimeError('input failure')
        if failure == 'signal':
            handlers[runner.signal.SIGTERM](None, None)
            after_signal.append(len(rig.sdk.calls))
        return original_snapshot(controller)
    monkeypatch.setattr(runner, 'input_snapshot', snapshot)
    if failure == 'disk':
        def disk(*args): raise OSError('disk failure')
        monkeypatch.setattr(runner, 'atomic_json', disk)
    args = SimpleNamespace(session_dir=None, episode_dir=tmp_path)
    config = {'can_interface': 'mock', 'piper_gamepad': {'runtime': str(tmp_path), 'control_hz': 200}}
    if failure == 'signal':
        assert runner.run(args, config) == 0
        assert all(n == 'MotionCtrl_1' for n, _, _ in rig.sdk.calls[after_signal[0]:])
    else:
        with pytest.raises((RuntimeError, OSError), match=failure + ' failure'):
            runner.run(args, config)
    assert quit_calls
    assert any(n == 'MotionCtrl_1' and a == (1, 0, 0) for n, a, _ in rig.sdk.calls)
    assert not rig.ctl.guard.thread.is_alive()


def test_robot_and_deploy_copies_match():
    for name in ('safe_teleop.py', 'piper_safety.py', 'piper_gamepad_teleop.py'):
        robot = ROOT.parent / 'robots/unitree/home/unitree/heterovla-collection/onboard' / name
        assert robot.read_bytes() == (ROOT / 'remote/unitree' / name).read_bytes()
