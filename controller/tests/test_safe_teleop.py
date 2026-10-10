"""Exercise deployed joint control without hardware, cuRobo or pygame."""
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'remote/unitree'))
from piper_safety import ENABLE_IDS, FEEDBACK_IDS, SafetyGuard
from safe_teleop import safe_controller


class Piper:
    def __init__(self, degrees=(0, 0, 0, 0, 0, 0), width=0):
        self.logger = logging.getLogger('fake-piper-' + str(id(self)))
        self.calls = []
        self.fresh = set(FEEDBACK_IDS + ENABLE_IDS)
        self.enabled = [False] * 6
        self.joints = SimpleNamespace(joint_state=SimpleNamespace(**{
            'joint_' + str(i): int(q * 1000) for i, q in enumerate(degrees, 1)}))
        self.grip = SimpleNamespace(gripper_state=SimpleNamespace(grippers_angle=width))
        self.status = SimpleNamespace(arm_status=SimpleNamespace(arm_status=0, err_code=0))

    def feedback_fresh(self, ids, timeout): return set(ids) <= self.fresh
    def GetArmJointMsgs(self): return self.joints
    def GetArmGripperMsgs(self): return self.grip
    def GetArmStatus(self): return self.status
    def GetArmEnableStatus(self): return self.enabled

    def __getattr__(self, name):
        def command(*args, **kwargs): self.calls.append((name, args, kwargs))
        return command


def controller(piper=None):
    class Base:
        def __init__(self): self.arm_connected = self.arm_enabled = False
    ctl = safe_controller(Base)()
    ctl.interface = piper or Piper()
    ctl.deadzone = .1
    ctl.joint_limits = np.radians([(-150, 150), (0, 180), (-170, 0),
                                  (-100, 100), (-70, 70), (-170, 170)])
    ctl.gripper_max_width = .07
    ctl.joint_angles = np.zeros(6)
    ctl.gripper_state = 0.
    ctl._axes = dict.fromkeys(('left_x', 'left_y', 'right_x', 'right_y',
                              'left_trigger', 'right_trigger'), 0.)
    ctl._apply_deadzone = lambda value: value if abs(value) > .1 else 0.
    ctl.attach_safety(SafetyGuard(ctl.interface))
    return ctl


def test_startup_opens_receiver_without_enabling_or_homing():
    ctl = controller()
    ctl.connect_feedback(); ctl.connect_feedback()
    assert ctl.interface.calls == [('ConnectPort', (), {'piper_init': False})]
    assert ctl.command_inhibited and not ctl.arm_enabled


def test_hardware_enabled_does_not_claim_teleop_enabled():
    ctl = controller(); ctl.interface.enabled = [True] * 6
    assert ctl.feedback_status() == {'motor_enabled': [True] * 6, 'feedback_error': None}
    assert not ctl.arm_enabled and ctl.command_inhibited
    assert not ctl.send_safe_command()
    assert ctl.interface.calls == []


def test_measured_bad_zero_blocks_home_before_any_motion_command():
    ctl = controller(Piper((-1.989, -2.494, 8.766, -13.641, 22.336, 27.033), -280))
    with pytest.raises(ValueError, match='J2=.*J3='): ctl._connect_and_enable_arm()
    assert ctl.command_inhibited and ctl._arm_deadline is None
    assert [name for name, _, _ in ctl.interface.calls] == ['ConnectPort']
    assert '核查零位' in ctl.feedback_status()['feedback_error']


def test_negative_gripper_blocks_enable_even_with_valid_joints():
    ctl = controller(Piper(width=-280))
    with pytest.raises(ValueError, match='-0.280mm'): ctl._connect_and_enable_arm()
    assert ctl.command_inhibited
    assert [name for name, _, _ in ctl.interface.calls] == ['ConnectPort']


@pytest.mark.parametrize('missing', FEEDBACK_IDS)
def test_missing_one_feedback_pair_blocks_home(missing):
    ctl = controller(); ctl.interface.fresh.remove(missing)
    with pytest.raises(ValueError, match='反馈过期'): ctl._connect_and_enable_arm()
    assert ctl.command_inhibited


def test_home_seeds_current_pose_and_waits_for_driver_enable_feedback():
    ctl = controller(Piper((1, 20, -30, 4, 5, 6), 35000))
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    assert not ctl.arm_enabled
    joint_call = next(call for call in ctl.interface.calls if call[0] == 'JointCtrl')
    assert joint_call[1] == (1000, 20000, -30000, 4000, 5000, 6000)
    ctl.interface.enabled = [True] * 6
    ctl._finish_enable(ctl.guard.clock())
    assert ctl.arm_enabled and not ctl.command_inhibited
    np.testing.assert_allclose(ctl.joint_angles, np.radians([1, 20, -30, 4, 5, 6]))
    assert ctl.gripper_state == 50.
    assert not any(call[0] == 'ResetPiper' for call in ctl.interface.calls)


def test_home_with_non_neutral_stick_never_acknowledges():
    ctl = controller(); ctl._axes['left_x'] = .5
    with pytest.raises(ValueError, match='松开所有'): ctl._connect_and_enable_arm()
    assert ctl.command_inhibited and ctl.interface.calls == []


def test_joint_and_gripper_leads_remain_bounded_with_stalled_feedback():
    ctl = controller(); ctl._control_dt = .02
    ctl._axes.update(left_y=-1., right_y=-1., right_trigger=1.)
    actual = np.zeros(6)
    for _ in range(1000): ctl._update_targets(actual, 0.)
    assert np.max(np.abs(ctl.joint_angles - actual)) <= ctl.joint_lead
    assert ctl.gripper_state / 100. * .07 <= ctl.gripper_lead
    ctl._axes = dict.fromkeys(ctl._axes, 0.)
    ctl._update_targets(actual, 0.)
    np.testing.assert_array_equal(ctl.joint_angles, actual)
    assert ctl.gripper_state == 0.


def test_large_target_latches_stop_without_sending_joint_target():
    ctl = controller(); ctl.interface.enabled = [True] * 6
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    ctl.interface.calls.clear(); ctl.joint_angles[0] = np.radians(10)
    assert not ctl.send_safe_command()
    assert ctl.command_inhibited and ctl.guard.stop_requested
    assert [c[0] for c in ctl.interface.calls] == ['MotionCtrl_1']


def test_cartesian_control_cannot_be_enabled():
    with pytest.raises(ValueError, match='Cartesian'): safe_controller(object, joint_only=False)


def test_new_feedback_between_planning_and_sending_does_not_latch_estop():
    ctl = controller(Piper((1, 20, -30, 4, 5, 6), 35000))
    ctl.interface.enabled = [True] * 6
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    ctl._control_dt = .02; ctl._axes['left_x'] = -1.
    q, width = ctl._read_feedback(True)
    for _ in range(20): ctl._update_targets(q, width)
    assert np.isclose(ctl.joint_angles[0], np.radians(3))
    # A new CAN frame moves by only 0.001 degree in the opposite direction.
    ctl.interface.joints.joint_state.joint_1 = 999
    ctl.interface.calls.clear()
    assert ctl.send_safe_command()
    assert not ctl.command_inhibited and not ctl.guard.stop_requested
    joint_call = next(call for call in ctl.interface.calls if call[0] == 'JointCtrl')
    assert joint_call[1][0] <= 2999


def test_gripper_feedback_change_at_lead_boundary_does_not_latch_estop():
    ctl = controller(Piper((0, 20, -30, 0, 0, 0), 35000))
    ctl.interface.enabled = [True] * 6
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    ctl._control_dt = .02; ctl._axes['right_trigger'] = 1.
    q, width = ctl._read_feedback(True)
    for _ in range(20): ctl._update_targets(q, width)
    ctl.interface.grip.gripper_state.grippers_angle = 34999
    assert ctl.send_safe_command()
    assert not ctl.guard.stop_requested
    args = next(args for name, args, _ in reversed(ctl.interface.calls) if name == 'GripperCtrl')
    assert args[0] <= 35999


def test_drift_of_joint_without_stick_input_still_stops():
    ctl = controller(Piper((0, 20, -30, 0, 0, 0), 35000))
    ctl.interface.enabled = [True] * 6
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    ctl.interface.joints.joint_state.joint_1 = 3000
    ctl.interface.calls.clear()
    assert not ctl.send_safe_command()
    assert ctl.guard.stop_requested
    assert [name for name, _, _ in ctl.interface.calls] == ['MotionCtrl_1']


@pytest.mark.parametrize('axis,index,sign', [
    ('left_x', 0, -1), ('left_y', 1, -1),
    ('right_y', 2, 1), ('right_x', 5, 1),
])
def test_each_stick_sends_bounded_joint_commands(axis, index, sign):
    ctl = controller(Piper((0, 20, -30, 0, 0, 0), 35000))
    ctl.interface.enabled = [True] * 6
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    ctl._control_dt = .005; ctl._axes[axis] = .5
    for _ in range(200):
        assert ctl.send_safe_command()
        args = next(args for name, args, _ in reversed(ctl.interface.calls) if name == 'JointCtrl')
        for i, value in enumerate(args, 1):
            setattr(ctl.interface.joints.joint_state, 'joint_' + str(i), value)
    assert sign * (args[index] - [0, 20000, -30000, 0, 0, 0][index]) > 1000
    assert not ctl.guard.stop_requested


@pytest.mark.parametrize('hat,index,sign', [((1, 0), 3, 1), ((0, 1), 4, -1)])
def test_dpad_sends_wrist_joint_commands(hat, index, sign):
    ctl = controller(Piper((0, 20, -30, 0, 0, 0), 35000))
    ctl.interface.enabled = [True] * 6
    ctl._connect_and_enable_arm(); ctl._finish_enable(ctl.guard.clock())
    ctl._control_dt = .02; ctl._hat = hat
    assert ctl.send_safe_command()
    args = next(args for name, args, _ in reversed(ctl.interface.calls) if name == 'JointCtrl')
    assert sign * args[index] > 0


def test_unverified_calibration_blocks_home_even_if_feedback_moves_into_limits():
    ctl = controller(); ctl.calibration_fault = '零位校准尚未通过'
    with pytest.raises(ValueError, match='零位校准尚未通过'): ctl._connect_and_enable_arm()
    assert not ctl.send_safe_command()
    assert ctl.interface.calls == []
    assert ctl.command_inhibited
