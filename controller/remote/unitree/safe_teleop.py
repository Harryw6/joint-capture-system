"""Feedback-bounded joint teleoperation; no unguarded upstream motion shortcuts."""
import math
import time
import numpy as np
from piper_safety import ENABLE_IDS, FEEDBACK_IDS


def safe_controller(base, joint_only=True):
    if not joint_only:
        raise ValueError('Cartesian teleoperation is disabled until separately validated')

    class ContinuousTeleop(base):
        joint_lead = math.radians(2.)
        gripper_lead = .001  # metres; conservative commissioning defaults

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.speed_factors = [.25, .5, 1.]
            self.speed_factor_index = 1
            self.movement_speeds = [10, 20, 30]
            self.movement_speed_index = 1
            self.up_level_mode = self.low_level_mode = 'joint'
            self.command_mode = 0x00
            self.guard = None
            self.command_inhibited = True
            self.safety_fault = '松开所有摇杆、方向键和扳机，按 Home 确认遥操'
            self.ik_error = None
            self._last_control_tick = time.monotonic()
            self._control_dt = 0.
            self._previous_direction = np.zeros(6)
            self._previous_grip = 0.
            self._axes = {}
            self._hat = (0, 0)
            self._arm_deadline = None
            self._last_enable = -math.inf

        def attach_safety(self, guard):
            self.guard = guard
            self._sync_guard()

        def _sync_guard(self):
            if self.guard is not None:
                self.command_inhibited = self.guard.inhibited
                self.safety_fault = self.guard.fault

        def safety_stop(self, reason):
            self._arm_deadline = None
            self.command_inhibited = True
            self.safety_fault = str(reason)
            if self.guard is not None:
                self.guard.trip(reason)
                self._sync_guard()

        def _read_inputs(self):
            names = ('left_x', 'left_y', 'right_x', 'right_y', 'left_trigger', 'right_trigger')
            axes = {name: float(self._get_axis_value(name)) for name in names}
            if any(not math.isfinite(v) or not -1 <= v <= 1 for v in axes.values()):
                raise ValueError('手柄输入无效')
            if any(axes[n] < 0 for n in names[-2:]):
                raise ValueError('扳机映射无效')
            hat = tuple(self._get_hat_value('dpad'))
            if len(hat) != 2 or any(v not in (-1, 0, 1) for v in hat):
                raise ValueError('方向键输入无效')
            self._axes, self._hat = axes, hat

        def _neutral(self):
            return (bool(self._axes) and
                    all(abs(self._axes[n]) <= self.deadzone for n in
                        ('left_x', 'left_y', 'right_x', 'right_y')) and
                    all(self._axes[n] <= .05 for n in ('left_trigger', 'right_trigger')) and
                    self._hat == (0, 0))

        def _read_feedback(self, require_enabled=False):
            if not self.interface.feedback_fresh(FEEDBACK_IDS, .25):
                raise ValueError('CAN 关节、夹爪或状态反馈过期')
            joints = self.interface.GetArmJointMsgs()
            q = np.radians([float(getattr(joints.joint_state, 'joint_' + str(i))) / 1000.
                            for i in range(1, 7)])
            limits = np.asarray(self.joint_limits, dtype=float)
            if (limits.shape != (6, 2) or not np.isfinite(limits).all()
                    or not np.isfinite(q).all() or np.any(q < limits[:, 0])
                    or np.any(q > limits[:, 1])):
                raise ValueError('关节反馈无效或超限')
            width = float(self.interface.GetArmGripperMsgs().gripper_state.grippers_angle) / 1e6
            if not math.isfinite(width) or not 0 <= width <= self.gripper_max_width:
                raise ValueError('夹爪反馈无效')
            status = self.interface.GetArmStatus().arm_status
            if status.arm_status != 0 or status.err_code != 0:
                raise ValueError('机械臂报告故障或急停；先现场核查并解除，再按 Home 确认')
            if require_enabled:
                if (not self.interface.feedback_fresh(ENABLE_IDS, .25) or
                        not all(self.interface.GetArmEnableStatus())):
                    raise ValueError('驱动反馈过期或机械臂失去使能')
            return q, width

        def _seed_feedback(self, q, width):
            self.joint_angles = q.copy()
            self.gripper_state = width / self.gripper_max_width * 100.
            self._previous_direction[:] = 0
            self._previous_grip = 0.

        def _connect_and_enable_arm(self):
            if self.guard is None:
                raise RuntimeError('安全监控未初始化')
            if not self._neutral():
                raise ValueError('请先松开所有摇杆、方向键和扳机')
            with self.guard.lock:
                if not self.arm_connected:
                    self.guard.call('ConnectPort', piper_init=False)
                    self.arm_connected = True
                if self.guard.active:
                    self._read_feedback()
                self.guard.acknowledge()
                self._arm_deadline = time.monotonic() + 3.
                self._last_enable = -math.inf
                self.safety_fault = '等待新鲜反馈并确认使能；请保持手柄中立'

        def _finish_enable(self, now):
            if not self._neutral():
                raise ValueError('使能期间手柄离开中立位置')
            if now > self._arm_deadline:
                raise ValueError('使能超时，请检查 CAN、供电和机械臂状态')
            if not self.interface.feedback_fresh(FEEDBACK_IDS + ENABLE_IDS, .25):
                return
            with self.guard.lock:
                if self.guard.stop_requested:
                    raise ValueError('使能期间触发保护，请重新核查并按 Home 确认')
                q, width = self._read_feedback()
                self.guard.active = True
                self.guard.last_tick = now
                if now - self._last_enable >= .1:
                    # Replace any old firmware target BEFORE enabling, without homing.
                    self.guard.call('ModeCtrl', 0x01, 0x01, 10, 0x00)
                    self.guard.call('JointCtrl', *np.round(np.degrees(q) * 1000).astype(int).tolist())
                    self.guard.call('GripperCtrl', round(width * 1e6), 1000, 0x01, 0)
                    self.guard.call('EnableArm', 7)
                    self._last_enable = now
                if not all(self.interface.GetArmEnableStatus()):
                    return
                self._seed_feedback(q, width)
                self.arm_enabled = True
                self._arm_deadline = None
                self._sync_guard()

        def _handle_button_events(self, events):
            if 'home' in events:
                if self._arm_deadline is not None or (self.arm_enabled and not self.command_inhibited):
                    self.safety_stop('Home 暂停：已请求停止；现场确认后再恢复')
                else:
                    self._connect_and_enable_arm()
                return
            if any(name in events for name in ('b', 'y', 'back')):
                self.ik_error = '安全关节遥操已禁用回零、点位回放和 MIT 模式切换'

        def update(self):
            self.up_level_mode = self.low_level_mode = 'joint'
            self.command_mode = 0x00
            now = time.monotonic()
            dt = now - self._last_control_tick
            self._last_control_tick = now
            self._control_dt = min(.02, max(0., dt))
            try:
                if self.guard is None:
                    raise RuntimeError('安全监控未初始化')
                self.guard.poll()
                self._sync_guard()
                if self.guard.active and (dt < 0 or dt > self.guard.loop_timeout):
                    raise ValueError('遥操循环超时，请重新确认')
                connected = super()._handle_joystick_events()
                if not connected:
                    if self.guard.active or self._arm_deadline is not None:
                        self.safety_stop('手柄断连，已请求停止')
                    return
                self._read_inputs()
                events = {}
                for name, button in self.buttons.items():
                    index = self.button_map.get(name)
                    if index is not None and index < self.joystick.get_numbuttons():
                        if button.update(self.joystick.get_button(index)):
                            events[name] = True
                self._handle_button_events(events)
                self._check_button_long_press()
                if self._arm_deadline is not None:
                    self._finish_enable(now)
                self._sync_guard()
                if self.guard.active:
                    if dt < 0 or dt > self.guard.loop_timeout:
                        raise ValueError('遥操循环超时，请重新确认')
                    self.guard.last_tick = now
                if self.arm_enabled and not self.command_inhibited:
                    q, width = self._read_feedback(require_enabled=True)
                    self._update_targets(q, width)
                    self._joint_to_pose()
            except Exception as exc:
                self.safety_stop(str(exc))

        def _update_targets(self, actual, width):
            a = {n: self._apply_deadzone(v) for n, v in self._axes.items()}
            direction = np.array([-a['left_x'], -a['left_y'], a['right_y'],
                                  self._hat[0], -self._hat[1], a['right_x']])
            changed = np.sign(direction) != np.sign(self._previous_direction)
            self.joint_angles[changed] = actual[changed]
            step = math.radians(20.) * self._control_dt * self.speed_factors[self.speed_factor_index]
            target = self.joint_angles + direction * step
            active = direction != 0
            target[active] = np.clip(target[active], actual[active] - self.joint_lead,
                                     actual[active] + self.joint_lead)
            limits = np.asarray(self.joint_limits)
            self.joint_angles = np.clip(target, limits[:, 0], limits[:, 1])
            self._previous_direction = direction
            # No trigger jumps; use bounded, time-based gripper motion.
            grip = self._axes['right_trigger'] if self._axes['right_trigger'] > .05 else 0.
            grip -= self._axes['left_trigger'] if self._axes['left_trigger'] > .05 else 0.
            target_width = self.gripper_state / 100. * self.gripper_max_width
            if np.sign(grip) != np.sign(self._previous_grip):
                target_width = width
            if grip:
                target_width += grip * .01 * self._control_dt
                target_width = float(np.clip(target_width, width - self.gripper_lead, width + self.gripper_lead))
            self.gripper_state = float(np.clip(target_width, 0, self.gripper_max_width)) / self.gripper_max_width * 100.
            self._previous_grip = grip

        def send_safe_command(self):
            if self.guard is None:
                self.safety_stop('安全监控未初始化')
                return False
            try:
                with self.guard.lock:
                    self.guard.poll()
                    self._sync_guard()
                    if self.command_inhibited or not self.arm_enabled or self._arm_deadline is not None:
                        return False
                    q, width = self._read_feedback(require_enabled=True)
                    target = np.asarray(self.joint_angles)
                    limits = np.asarray(self.joint_limits)
                    grip = self.gripper_state / 100. * self.gripper_max_width
                    if (target.shape != (6,) or not np.isfinite(target).all() or
                            np.any(target < limits[:, 0]) or np.any(target > limits[:, 1]) or
                            np.any(np.abs(target - q) > self.joint_lead + 1e-8) or
                            not math.isfinite(grip) or not 0 <= grip <= self.gripper_max_width or
                            abs(grip - width) > self.gripper_lead + 1e-8):
                        raise ValueError('目标与实际位置偏差过大，已请求停止')
                    self.guard.call('ModeCtrl', 0x01, 0x01, self.movement_speeds[self.movement_speed_index], 0x00)
                    self.guard.call('JointCtrl', *np.round(np.degrees(target) * 1000).astype(int).tolist())
                    self.guard.call('GripperCtrl', round(grip * 1e6), 3000, 0x01, 0)
                    return True  # Submitted without detected SDK error, not completion.
            except Exception as exc:
                self.safety_stop('发送失败：' + str(exc))
                return False

        def _go_home(self):
            self.safety_stop('回零快捷键已禁用')

        def _go_home_and_disable(self):
            self.safety_stop('已请求停止，不执行回零或自动复位')

        def _toggle_command_mode(self):
            self.command_mode = 0x00

        def _toggle_up_level_mode(self):
            self.up_level_mode = 'joint'

        def _toggle_low_level_mode(self):
            self.low_level_mode = 'joint'

        def _pose_to_joint(self, xyz, orientation):
            return

        def _save_position(self):
            return

        def _restore_previous_position(self):
            self.safety_stop('点位回放已禁用')

    return ContinuousTeleop
