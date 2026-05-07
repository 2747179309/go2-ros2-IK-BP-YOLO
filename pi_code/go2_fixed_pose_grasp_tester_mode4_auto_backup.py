#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import threading
from pathlib import Path
import re

import cv2
import numpy as np
import pyrealsense2 as rs
import serial
from math import sqrt, atan2, acos, degrees, radians, sin, cos

if not hasattr(np, "bool"):
    np.bool = bool

try:
    import torch
    import torch.nn as nn
except Exception:
    torch = None
    nn = None

try:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.go2.sport.sport_client import SportClient
except Exception:
    ChannelFactoryInitialize = None
    SportClient = None

from ultralytics import YOLO
from pitch_predictor import PitchPredictor


# === RECORDED_FIXED_GRASP_PULSES START ===
RECORDED_FIXED_GRASP_PULSES = {1: 370, 2: 488, 3: 156, 4: 613, 5: 630, 6: 540}
# === RECORDED_FIXED_GRASP_PULSES END ===


def _serialize_int_dict(d):
    return '{' + ', '.join(f"{int(k)}: {int(v)}" for k, v in sorted(d.items())) + '}'


def get_recorded_fixed_grasp_pulses():
    return dict(RECORDED_FIXED_GRASP_PULSES)


def persist_recorded_fixed_grasp_pulses(pulses, file_path=None):
    global RECORDED_FIXED_GRASP_PULSES
    file_path = file_path or os.path.abspath(__file__)
    path = Path(file_path)
    normalized = {int(k): int(v) for k, v in pulses.items()}
    text = path.read_text(encoding='utf-8')
    pattern = r"(# === RECORDED_FIXED_GRASP_PULSES START ===\n)RECORDED_FIXED_GRASP_PULSES = .*?(\n# === RECORDED_FIXED_GRASP_PULSES END ===)"
    replacement = r"\1RECORDED_FIXED_GRASP_PULSES = " + _serialize_int_dict(normalized) + r"\2"
    new_text, count = re.subn(pattern, replacement, text, flags=re.S)
    if count != 1:
        raise RuntimeError('Failed to locate RECORDED_FIXED_GRASP_PULSES block in source file.')
    path.write_text(new_text, encoding='utf-8')
    RECORDED_FIXED_GRASP_PULSES = dict(normalized)


def prompt_manual_fixed_grasp_pulses(defaults=None):
    defaults = defaults or RECORDED_FIXED_GRASP_PULSES
    print('[record] automatic read failed or is unsupported by the controller. Falling back to manual input.')
    pulses = {}
    for sid in range(1, 7):
        default = int(defaults.get(sid, 500))
        while True:
            raw = input(f'Enter pulse for servo {sid} [default {default}]: ').strip()
            if raw == '':
                pulses[sid] = default
                break
            try:
                pulses[sid] = int(raw)
                break
            except ValueError:
                print('Invalid integer, try again.')
    return pulses

class StandardScalerNP:
    def __init__(self):
        self.mean_ = None
        self.std_ = None

    @classmethod
    def from_dict(cls, d):
        obj = cls()
        obj.mean_ = np.asarray(d['mean'], dtype=np.float32).reshape(1, -1)
        obj.std_ = np.asarray(d['std'], dtype=np.float32).reshape(1, -1)
        obj.std_[obj.std_ < 1e-12] = 1.0
        return obj

    def transform(self, x):
        x = np.asarray(x, dtype=np.float32)
        return (x - self.mean_) / self.std_

    def inverse_transform(self, x):
        x = np.asarray(x, dtype=np.float32)
        return x * self.std_ + self.mean_


class BPMLPRegressor(nn.Module):
    def __init__(self, input_dim, output_dim, hidden_layers, dropout=0.1):
        super().__init__()
        layers = []
        last_dim = input_dim
        for h in hidden_layers:
            layers.append(nn.Linear(last_dim, h))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(dropout))
            last_dim = h
        layers.append(nn.Linear(last_dim, output_dim))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


class BPServoPredictor:
    def __init__(self, model_path='bp_model.pth'):
        if torch is None or nn is None:
            raise RuntimeError('PyTorch is unavailable, but mode3 BP grasp requires torch to load bp_model.pth.')
        if not os.path.isabs(model_path):
            model_path = os.path.join(os.path.dirname(__file__), model_path)
        self.model_path = model_path
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f'BP model file not found: {self.model_path}')

        checkpoint = torch.load(self.model_path, map_location='cpu')
        self.input_dim = int(checkpoint['input_dim'])
        self.output_dim = int(checkpoint['output_dim'])
        self.hidden_layers = [int(v) for v in checkpoint['hidden_layers']]
        self.target_cols = list(checkpoint.get('target_cols', ['ID3', 'ID4', 'ID5', 'ID6']))
        self.x_scaler = StandardScalerNP.from_dict(checkpoint['x_scaler'])
        self.y_scaler = StandardScalerNP.from_dict(checkpoint['y_scaler'])

        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.dropout = float(checkpoint.get('dropout', 0.1))
        self.model = BPMLPRegressor(self.input_dim, self.output_dim, self.hidden_layers, dropout=self.dropout).to(self.device)
        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.model.eval()
        print(f"[bp] loaded model: {self.model_path} | device={self.device} | hidden_layers={self.hidden_layers} | dropout={self.dropout}")

    def predict_servo_pulses(self, x_cm, y_cm, z_cm):
        x = np.asarray([[float(x_cm), float(y_cm), float(z_cm)]], dtype=np.float32)
        x_s = self.x_scaler.transform(x)
        with torch.no_grad():
            x_t = torch.from_numpy(x_s).to(self.device)
            y_s = self.model(x_t).detach().cpu().numpy()
        y = self.y_scaler.inverse_transform(y_s).reshape(-1)
        out = {}
        for name, value in zip(self.target_cols, y):
            sid = int(str(name).replace('ID', '').strip())
            out[sid] = int(round(float(value)))
        return out



def run_record_fixed_pose_mode(arm_port='/dev/ttyUSB0', arm_baudrate=9600, pitch_model_path='pitch_model_py.mat'):
    arm = ArmPiFPVController(port=arm_port, baudrate=arm_baudrate, model_path=pitch_model_path)
    try:
        print('\n[mode] Read position / record current servo parameters')
        current_pulses = get_recorded_fixed_grasp_pulses()
        print(f'[record] current saved fixed grasp pulses: {current_pulses}')
        input('[record] Please manually place the arm at the fruit position, then press Enter to read current servo parameters...')
        pulses = arm.bus.read_all_servo_positions()
        if pulses is None or any(v is None for v in pulses.values()):
            pulses = prompt_manual_fixed_grasp_pulses(current_pulses)
        print(f'[record] captured pulses: {pulses}')
        confirm = input("[record] Record current servo parameters permanently into this code? (y/n): ").strip().lower()
        if confirm in ('y', 'yes'):
            persist_recorded_fixed_grasp_pulses(pulses)
            print('[record] fixed grasp pulses saved into source code successfully.')
        else:
            print('[record] cancelled; source code not modified.')
    finally:
        arm.close()


def _build_fixed_target_pulses(arm_controller, fixed_pulses, gripper_angle):
    target = dict(fixed_pulses)
    target[1] = arm_controller.kin.angle_to_pulse(1, gripper_angle)
    return arm_controller._clip_servos(target)


def execute_fixed_grasp_sequence(arm_controller, fixed_pulses, gripper_angle, close_pulse, home_pulses, home_open_pulses, wait1_pulses, step_time_ms, finish_pulses=None):
    target = _build_fixed_target_pulses(arm_controller, fixed_pulses, gripper_angle)
    print(f'[fixed-grasp] moving to recorded target pulses: {target}')
    arm_controller.bus.servo_move(target, 2000)
    time.sleep(2.2)

    print('[fixed-grasp] closing gripper...')
    arm_controller.bus.servo_move({1: close_pulse}, int(step_time_ms))
    time.sleep(step_time_ms / 1000.0 * 1.1)

    print('[fixed-grasp] returning to home...')
    arm_controller.bus.servo_move(home_pulses, 2000)
    time.sleep(2.2)

    print('[fixed-grasp] opening gripper...')
    arm_controller.bus.servo_move(home_open_pulses, 1200)
    time.sleep(1.5)

    print('[fixed-grasp] restoring post-release pose...')
    arm_controller.bus.servo_move({1: close_pulse}, int(step_time_ms))
    time.sleep(1.0)

    if finish_pulses is None:
        finish_pulses = wait1_pulses

    print(f'[fixed-grasp] moving to finish/wait pose: {finish_pulses}')
    arm_controller.bus.servo_move(finish_pulses, 2000)
    time.sleep(2.2)


def run_fixed_grasp_only_mode(arm_port='/dev/ttyUSB0', arm_baudrate=9600, pitch_model_path='pitch_model_py.mat'):
    arm = ArmPiFPVController(port=arm_port, baudrate=arm_baudrate, model_path=pitch_model_path)
    try:
        print('\n[mode] Execute fixed-position grasp only')
        current_pulses = get_recorded_fixed_grasp_pulses()
        print(f'[fixed-grasp] using recorded pulses: {current_pulses}')
        input('[fixed-grasp] Press Enter to start the fixed-position grasp sequence...')
        close_pulse = arm.kin.angle_to_pulse(1, 25)
        close_pulse = int(np.clip(close_pulse, arm.servo_limits[1][0], arm.servo_limits[1][1]))
        execute_fixed_grasp_sequence(
            arm_controller=arm,
            fixed_pulses=current_pulses,
            gripper_angle=120,
            close_pulse=close_pulse,
            home_pulses={1: 550, 2: 501, 3: 781, 4: 275, 5: 423, 6: 499},
            home_open_pulses={1: 380, 2: 501, 3: 781, 4: 275, 5: 423, 6: 499},
            wait1_pulses={1: 493, 2: 499, 3: 787, 4: 376, 5: 376, 6: 604},
            finish_pulses={1: 510, 2: 499, 3: 148, 4: 886, 5: 854, 6: 486},
            step_time_ms=800,
        )
        print('[fixed-grasp] sequence completed.')
    finally:
        arm.close()



class BusServoController:
    def __init__(self, port='/dev/ttyUSB0', baudrate=9600, timeout=0.5):
        self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)

    def close(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

    def _send_command(self, cmd, params):
        frame = bytearray()
        frame.extend([0x55, 0x55])
        frame.append(len(params) + 2)
        frame.append(cmd)
        frame.extend(params)
        self.ser.write(frame)
        if cmd in (0x0F, 0x15):
            return self._parse_response()
        return None

    def _parse_response(self):
        header = self.ser.read(2)
        if header != b'UU':
            return None
        length_b = self.ser.read(1)
        cmd_b = self.ser.read(1)
        if len(length_b) != 1 or len(cmd_b) != 1:
            return None
        length = length_b[0]
        cmd = cmd_b[0]
        data = self.ser.read(max(0, length - 2))

        if cmd == 0x0F:
            if len(data) >= 2:
                return (data[1] << 8) + data[0]
            return None
        elif cmd == 0x15:
            positions = {}
            if len(data) < 1:
                return None
            servo_num = data[0]
            for i in range(servo_num):
                idx = 1 + i * 3
                if idx + 2 >= len(data):
                    break
                servo_id = data[idx]
                pos = (data[idx + 2] << 8) + data[idx + 1]
                positions[servo_id] = pos
            return positions if positions else None
        return None

    def servo_move(self, servos: dict, time_ms: int):
        params = [len(servos), time_ms & 0xFF, (time_ms >> 8) & 0xFF]
        for sid, pos in servos.items():
            pos = int(max(0, min(1000, int(pos))))
            params.extend([int(sid), pos & 0xFF, (pos >> 8) & 0xFF])
        self._send_command(0x03, params)

    def get_voltage(self):
        return self._send_command(0x0F, [])

    def read_servo_positions(self, servo_ids):
        params = [len(servo_ids)]
        params.extend([int(sid) for sid in servo_ids])
        return self._send_command(0x15, params)

    def read_servo_position(self, sid: int):
        positions = self.read_servo_positions([sid])
        if not positions:
            return None
        return positions.get(int(sid))

    def read_all_servo_positions(self):
        positions = self.read_servo_positions([1, 2, 3, 4, 5, 6])
        if not positions:
            return None
        pulses = {}
        any_success = False
        for sid in range(1, 7):
            pos = positions.get(sid)
            pulses[sid] = pos
            any_success = any_success or (pos is not None)
        return pulses if any_success else None


class FiveDOFKinematics:
    def __init__(self):
        self.link_lengths = {
            'base_height': 5.8,
            'upper_arm': 10.0,
            'forearm': 9.5,
            'wrist_to_gripper': 8.3,
            'gripper_length': 11.4
        }
        self.joint_limits = {
            1: (0, 180), 2: (-120, 120), 3: (-120, 120),
            4: (-120, 120), 5: (-120, 120), 6: (-120, 120)
        }

    def angle_to_pulse(self, servo_id, angle):
        if servo_id == 1:
            pulse = int(1000 - (1000 / 180.0) * angle)
        else:
            pulse = int(500 + 500.0 * angle / 120.0)
        return max(0, min(1000, pulse))

    def inverse_kinematics(self, x, y, z, pitch, gripper_angle=150, wrist_roll=0):
        if y < 0:
            y = -y
            theta6 = -degrees(atan2(y, x))
        else:
            theta6 = degrees(atan2(y, x))

        r = sqrt(x**2 + y**2)
        end_effector_length = self.link_lengths['wrist_to_gripper'] + self.link_lengths['gripper_length']
        wrist_x = r - end_effector_length * cos(radians(pitch))
        wrist_z = z - self.link_lengths['base_height'] - end_effector_length * sin(radians(pitch))
        d = sqrt(wrist_x**2 + wrist_z**2)

        L2 = self.link_lengths['upper_arm']
        L3 = self.link_lengths['forearm']
        if d > L2 + L3 or d < abs(L2 - L3):
            return None

        try:
            alpha = atan2(wrist_z, wrist_x)
            cos_beta = (L2**2 + d**2 - L3**2) / (2 * L2 * d)
            cos_beta = np.clip(cos_beta, -1, 1)
            beta = acos(cos_beta)
            theta5 = degrees(alpha + beta)
        except Exception:
            return None

        try:
            cos_gamma = (L2**2 + L3**2 - d**2) / (2 * L2 * L3)
            cos_gamma = np.clip(cos_gamma, -1, 1)
            gamma = acos(cos_gamma)
            theta4 = degrees(gamma)
        except Exception:
            return None

        theta3 = -(pitch - theta5 + theta4)
        theta2 = wrist_roll
        theta1 = gripper_angle
        angles = [theta1, theta2, theta3, theta4, theta5, theta6]
        joint_ids = [1, 2, 3, 4, 5, 6]
        return {jid: self.angle_to_pulse(jid, angle) for angle, jid in zip(angles, joint_ids)}


class ArmPiFPVController:
    def __init__(self, port='/dev/ttyUSB0', baudrate=9600, model_path='pitch_model_py.mat'):
        self.bus = BusServoController(port=port, baudrate=baudrate)
        if not os.path.isabs(model_path):
            model_path = os.path.join(os.path.dirname(__file__), model_path)
        self.pitch_pred = PitchPredictor(model_path)
        self.kin = FiveDOFKinematics()
        self.servo_limits = {1:(10,800),2:(0,1000),3:(0,1000),4:(0,1000),5:(0,1000),6:(0,1000)}

    def close(self):
        self.bus.close()

    def _clip_servos(self, servos: dict) -> dict:
        out = {}
        for sid, pos in servos.items():
            mn, mx = self.servo_limits.get(sid, (0, 1000))
            out[sid] = int(np.clip(int(pos), mn, mx))
        return out

    def move_xyz(self, x, y, z, gripper_angle=150, wrist_roll=45, duration_ms=1500, verbose=True):
        pitch = float(self.pitch_pred.predict(x, y, z))
        servos = self.kin.inverse_kinematics(x, y, z, pitch, gripper_angle=gripper_angle, wrist_roll=wrist_roll)
        if servos is None:
            raise ValueError("IK failed (unreachable target).")
        servos[3] = servos[3] + 45
        servos = self._clip_servos(servos)
        if verbose:
            print(f"[target] x={x:.2f} y={y:.2f} z={z:.2f} cm | pitch={pitch:.3f} deg")
            print("[pulses]", servos)
        self.bus.servo_move(servos, int(duration_ms))
        time.sleep(duration_ms / 1000.0 * 1.1)
        return pitch, servos


class Go2SportController:
    _channel_initialized = False
    _channel_iface = None

    def __init__(self, iface='eth0', auto_prepare=True):
        if ChannelFactoryInitialize is None or SportClient is None:
            raise RuntimeError("unitree_sdk2py is unavailable. Use the same Python environment where the official SDK example runs.")
        iface = iface or ''
        if not Go2SportController._channel_initialized:
            if iface:
                ChannelFactoryInitialize(0, iface)
            else:
                ChannelFactoryInitialize(0)
            Go2SportController._channel_initialized = True
            Go2SportController._channel_iface = iface
            print(f"[go2-sdk] channel initialized on iface='{iface or 'default'}'")
        self.client = SportClient()
        self.client.SetTimeout(10.0)
        self.client.Init()
        print("[go2-sdk] SportClient ready")
        if auto_prepare:
            print("[go2-sdk] StandUp()")
            self.client.StandUp(); time.sleep(1.5)
            print("[go2-sdk] BalanceStand()")
            self.client.BalanceStand(); time.sleep(1.0)

    def publish(self, vx=0.0, vy=0.0, wz=0.0):
        return self.client.Move(float(vx), float(vy), float(wz))

    def stop(self, repeat=3, interval=0.03):
        for _ in range(max(1, int(repeat))):
            self.client.StopMove()
            time.sleep(interval)

    def close(self):
        try:
            self.stop()
        except Exception:
            pass


class RealSenseYOLOWithDepth:
    def __init__(self, model_path='bestn.engine', arm_port='/dev/ttyUSB0', arm_baudrate=9600,
                 pitch_model_path='pitch_model_py.mat', bp_model_path='bp_model.pth', imgsz=640, conf=0.25, display=True,
                 go2_iface='eth0', fixed_grasp_pulses=None, auto_demo_mode=False):
        self.model_path = str(model_path)
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.display = bool(display)
        self.model_suffix = Path(self.model_path).suffix.lower()
        self.use_onnx_dnn = self.model_suffix == '.onnx'
        self.device = 'cpu'
        if self.model_suffix in ('.pt', '.engine') and torch is not None and torch.cuda.is_available():
            self.device = 0

        self.model = YOLO(self.model_path, task='detect')
        print(f"[yolo] model={self.model_path} suffix={self.model_suffix} device={self.device} use_onnx_dnn={self.use_onnx_dnn}")

        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self.config.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 15)
        self.config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 15)
        try:
            self.pipeline_profile = self.pipeline.start(self.config)
        except Exception:
            ctx = rs.context()
            for dev in ctx.query_devices():
                dev.hardware_reset()
            time.sleep(2)
            self.pipeline_profile = self.pipeline.start(self.config)

        self.align_to = rs.stream.color
        self.align = rs.align(self.align_to)
        self.depth_scale = self._get_depth_scale()
        self.intrinsics = self._get_camera_intrinsics()

        self.prev_time = 0.0
        self.arm = ArmPiFPVController(port=arm_port, baudrate=arm_baudrate, model_path=pitch_model_path)
        self.bp_predictor = BPServoPredictor(model_path=bp_model_path)
        self.base = Go2SportController(iface=go2_iface, auto_prepare=True)
        print(f"[go2-sdk] direct control enabled on iface='{go2_iface}'")

        self._lock = threading.Lock()
        self._moving = False

        self.M_TO_CM = 100.0
        self.GRIPPER_ANGLE = 120
        self.GRIPPER_CLOSE_ANGLE = 25
        self.WRIST_ROLL = 45
        self.MOVE_TIME_MS = 1200
        self.FIXED_Z_CM = 20.0

        self.HOME_PULSES = {1: 550, 2: 501, 3: 781, 4: 275, 5: 423, 6: 499}
        self.HOME_OPEN_PULSES = {1: 380, 2: 501, 3: 781, 4: 275, 5: 423, 6: 499}
        self.WAIT2_PULSES = {1: 501, 2: 487, 3: 660, 4: 120, 5: 386, 6: 687}
        self.WAIT1_PULSES = {1: 493, 2: 499, 3: 787, 4: 376, 5: 376, 6: 604}
        self.FINISH_PULSES = {1: 510, 2: 499, 3: 148, 4: 886, 5: 854, 6: 486}
        self.GRIPPER_STEP_TIME_MS = 800
        self._last_close_pulse = self.arm.kin.angle_to_pulse(1, self.GRIPPER_CLOSE_ANGLE)
        self._last_close_pulse = int(np.clip(self._last_close_pulse, self.arm.servo_limits[1][0], self.arm.servo_limits[1][1]))
        self.fixed_grasp_pulses = dict(fixed_grasp_pulses or get_recorded_fixed_grasp_pulses())

        self.DESIRED_X_CM = 24.5
        self.DESIRED_Y_CM = -1.0
        self.DESIRED_Z_CM = 26.5

        self.VALID_X_RANGE_CM = (1.0, 200.0)
        self.VALID_ABS_Y_MAX_CM = 100.0
        self.VALID_Z_RANGE_CM = (0.0, 60.0)

        self.AUTO_PROMPT_COOLDOWN_S = 1.0
        self.AUTO_ALIGN_TIMEOUT_S = 15.0
        self.TARGET_LOST_TIMEOUT_S = 5.0
        self._last_prompt_t = 0.0
        self._align_prompt_active = False
        self._auto_align_active = False
        self._align_start_t = None
        self._aligned_frames = 0
        self._active_target = None
        self._active_target_last_seen_t = 0.0
        self._target_cm = None
        self._prompt_active = False
        self._grab_active = False
        self.MENU_RETURN_CHAR = '!'
        self.WINDOW_MENU_KEY = ord('m')
        self._return_to_menu_requested = False
        self._timeout_prompt_active = False
        self._backing_up = False
        self.auto_demo_mode = bool(auto_demo_mode)
        self.AUTO_DEMO_NO_FRUIT_TIMEOUT_S = 8.0
        self._no_fruit_since = None
        self.AUTO_DEMO_BACKUP_STEPS = 2
        self.AUTO_DEMO_BACKUP_VX = -0.26
        self.AUTO_DEMO_BACKUP_STEP_DURATION_S = 0.60
        self.AUTO_DEMO_BACKUP_STEP_PAUSE_S = 0.50

        self.X_DEADBAND_CM = 0.5
        self.Y_DEADBAND_CM = 0.5
        self.PIXEL_DEADBAND_PX = 18.0
        self.MAX_FORWARD_ERR_CM = 20.0
        self.MAX_LATERAL_ERR_CM = 20.0
        self.MAX_PIXEL_ERR_PX = 250.0
        self.MIN_VX = 0.09
        self.MAX_VX = 0.28
        self.MIN_VY = 0.09
        self.MAX_VY = 0.25
        self.MIN_WZ = 0.2
        self.MAX_WZ = 0.25
        self.PIXEL_TURN_PRIORITY_PX = 50.0
        self.ALIGN_SUCCESS_FRAMES = 3
        self.SUCCESS_X_CM = 1.0
        self.SUCCESS_Y_CM = 4.5
        self.SUCCESS_PIXEL_PX = 40.0
        self.TURN_STAGE_PX = 80.0
        self.TURN_STAGE_WZ = 0.36
        self.TURN_STAGE_VX = 0.07

    def close(self):
        try:
            if self.base is not None:
                self.base.close()
        except Exception:
            pass
        try:
            self.arm.close()
        except Exception:
            pass

    def _predict(self, color_image):
        kwargs = {'source': color_image, 'verbose': False, 'imgsz': self.imgsz, 'conf': self.conf}
        if self.use_onnx_dnn:
            kwargs['dnn'] = True
            kwargs['device'] = 'cpu'
        else:
            kwargs['device'] = self.device
        return self.model.predict(**kwargs)

    def _get_depth_scale(self):
        try:
            depth_sensor = self.pipeline_profile.get_device().first_depth_sensor()
            return depth_sensor.get_depth_scale()
        except Exception:
            return 0.001

    def _get_camera_intrinsics(self):
        try:
            color_profile = self.pipeline_profile.get_stream(rs.stream.color)
            return color_profile.as_video_stream_profile().intrinsics
        except Exception as e:
            print(f"[camera] intrinsics fallback due to: {e}")
            return rs.intrinsics(width=640, height=480, fx=615, fy=615, cx=320, cy=240, model=rs.distortion.none)

    def pixel_to_3d_xyz(self, depth_frame, pixel_x, pixel_y):
        depth_value = depth_frame.get_distance(pixel_x, pixel_y)
        if depth_value <= 0:
            return None
        x, y, z = rs.rs2_deproject_pixel_to_point(self.intrinsics, [pixel_x, pixel_y], depth_value)
        return round(float(x), 3), round(float(y), 3), round(float(z), 3)

    @staticmethod
    def _interp_speed(err, deadband, max_err, min_speed, max_speed):
        mag = abs(float(err))
        if mag <= deadband or max_err <= deadband:
            return 0.0
        ratio = min(1.0, (mag - deadband) / (max_err - deadband))
        speed = min_speed + ratio * (max_speed - min_speed)
        return speed if err > 0 else -speed

    def _is_candidate_valid(self, x_cm, y_cm, z_cm):
        return (self.VALID_X_RANGE_CM[0] <= x_cm <= self.VALID_X_RANGE_CM[1] and abs(y_cm) <= self.VALID_ABS_Y_MAX_CM and self.VALID_Z_RANGE_CM[0] <= z_cm <= self.VALID_Z_RANGE_CM[1])

    def _score_candidate(self, cand, frame_w, frame_h):
        move_cost_cm = float(np.hypot(cand['x_cm'] - self.DESIRED_X_CM, cand['y_cm'] - self.DESIRED_Y_CM))
        z_cost_cm = abs(cand['z_cm'] - self.DESIRED_Z_CM)
        cx = frame_w * 0.5
        cy = frame_h * 0.5
        center_cost = float(np.hypot((cand['pixel_x'] - cx) / max(cx, 1.0), (cand['pixel_y'] - cy) / max(cy, 1.0)))
        conf_cost = 1.0 - cand['conf']
        move_norm = min(1.0, move_cost_cm / 30.0)
        z_norm = min(1.0, z_cost_cm / 15.0)
        center_norm = min(1.0, center_cost / 1.2)
        conf_norm = min(1.0, max(0.0, conf_cost))
        return float(0.50 * move_norm + 0.25 * z_norm + 0.15 * center_norm + 0.10 * conf_norm)

    def _select_best_candidate(self, candidates, frame_w, frame_h):
        if not candidates:
            return None
        for c in candidates:
            c['score'] = self._score_candidate(c, frame_w, frame_h)
        return sorted(candidates, key=lambda x: x['score'])[0]

    def _select_locked_candidate(self, candidates):
        if not candidates or self._active_target is None:
            return None
        best = None
        best_lock_score = None
        for c in candidates:
            dp = np.hypot(c['pixel_x'] - self._active_target['pixel_x'], c['pixel_y'] - self._active_target['pixel_y'])
            dxyz = np.linalg.norm(np.array([c['x_cm'], c['y_cm'], c['z_cm']]) - np.array([self._active_target['x_cm'], self._active_target['y_cm'], self._active_target['z_cm']]))
            lock_score = 0.70 * min(1.0, dp / 150.0) + 0.30 * min(1.0, dxyz / 25.0)
            if best is None or lock_score < best_lock_score:
                best = c
                best_lock_score = lock_score
        return best

    def _publish_base_cmd(self, vx, vy, wz=0.0):
        try:
            ret = self.base.publish(vx=vx, vy=vy, wz=wz)
            print(f"[go2-sdk] Move({vx:.3f}, {vy:.3f}, {wz:.3f}) ret={ret}")
        except Exception as e:
            print(f"[go2-sdk] publish failed: {e}")

    def _stop_base(self):
        try:
            self.base.stop()
        except Exception as e:
            print(f"[go2-sdk] stop failed: {e}")

    def _request_return_to_menu(self, reason=''):
        msg = '[mode3] return to mode selection requested'
        if reason:
            msg += f': {reason}'
        print(msg)
        self._stop_base()
        with self._lock:
            self._return_to_menu_requested = True
            self._auto_align_active = False
            self._align_prompt_active = False
            self._prompt_active = False
            self._active_target = None
            self._aligned_frames = 0

    def _should_return_to_menu(self):
        with self._lock:
            return bool(self._return_to_menu_requested) and not self._grab_active

    def _build_bp_target_pulses(self, xyz_cm):
        x_cm, y_cm, z_cm = [float(v) for v in xyz_cm]
        predicted = self.bp_predictor.predict_servo_pulses(x_cm, y_cm, z_cm)
        target = {2: int(self.fixed_grasp_pulses.get(2, 500))}
        for sid in (3, 4, 5, 6):
            if sid not in predicted:
                raise RuntimeError(f'BP predictor missing output for servo ID{sid}')
            target[sid] = int(predicted[sid])
        target = self.arm._clip_servos(target)
        print(f"[bp-grasp] xyz=({x_cm:.2f}, {y_cm:.2f}, {z_cm:.2f}) cm -> predicted target pulses: {target}")
        return target

    def _backup_two_steps(self, reason=''):
        prefix = '[auto-demo] backing up two steps'
        if reason:
            prefix += f' ({reason})'
        print(prefix)
        self._backing_up = True
        try:
            for step_idx in range(self.AUTO_DEMO_BACKUP_STEPS):
                t0 = time.time()
                while time.time() - t0 < self.AUTO_DEMO_BACKUP_STEP_DURATION_S:
                    self._publish_base_cmd(vx=self.AUTO_DEMO_BACKUP_VX, vy=0.0, wz=0.0)
                    time.sleep(0.05)
                self._stop_base()
                time.sleep(self.AUTO_DEMO_BACKUP_STEP_PAUSE_S)
                print(f"[auto-demo] backup step {step_idx + 1}/{self.AUTO_DEMO_BACKUP_STEPS} finished")
        finally:
            self._stop_base()
            self._backing_up = False
            self._last_prompt_t = time.time()
            self._no_fruit_since = None

    def _prompt_timeout_action_async(self, xyz_cm):
        def worker():
            try:
                while True:
                    ans = input(
                        f"[auto-align] timeout at xyz=({xyz_cm[0]:.1f}, {xyz_cm[1]:.1f}, {xyz_cm[2]:.1f}) cm. "
                        f"Choose: 1=direct grasp  2=realign  {self.MENU_RETURN_CHAR}=menu : "
                    ).strip().lower()
                    if ans == self.MENU_RETURN_CHAR:
                        self._request_return_to_menu('user typed menu char during timeout decision')
                        return
                    if ans in ('1', 'g', 'grasp', 'direct'):
                        with self._lock:
                            self._timeout_prompt_active = False
                            self._target_cm = tuple(xyz_cm)
                        self._grab_sequence_async(tuple(xyz_cm))
                        return
                    if ans in ('2', 'r', 'realign', 'again'):
                        self._backup_two_steps('timeout -> realign')
                        with self._lock:
                            self._timeout_prompt_active = False
                            self._auto_align_active = True
                            self._align_start_t = time.time()
                            self._aligned_frames = 0
                            self._last_prompt_t = time.time()
                        print('[auto-align] restart requested by user after timeout.')
                        return
                    print('Invalid choice. Enter 1, 2, or menu char.')
            finally:
                with self._lock:
                    if not self._auto_align_active and not self._grab_active:
                        self._timeout_prompt_active = False

        with self._lock:
            if self._timeout_prompt_active or self._grab_active or self._prompt_active:
                return
            self._timeout_prompt_active = True
        threading.Thread(target=worker, daemon=True).start()

    def _prompt_grab_async(self, xyz_cm):
        def worker():
            try:
                x_cm, y_cm, z_cm = [float(v) for v in xyz_cm]
                ans = input(
                    f"Auto-aligned target is near x={x_cm:.1f}cm, y={y_cm:.1f}cm, z={z_cm:.1f}cm. "
                    f"Execute BP grasp? (y/n, {self.MENU_RETURN_CHAR}=menu): "
                ).strip().lower()
                if ans == self.MENU_RETURN_CHAR:
                    self._request_return_to_menu('user typed menu char during grab confirmation')
                elif ans in ("y", "yes"):
                    self._grab_sequence_async(tuple(xyz_cm))
                else:
                    with self._lock:
                        self._target_cm = None
            finally:
                with self._lock:
                    self._prompt_active = False
        with self._lock:
            if self._prompt_active or self._grab_active or self._timeout_prompt_active:
                return
            self._prompt_active = True
        threading.Thread(target=worker, daemon=True).start()

    def _grab_sequence_async(self, xyz_cm):
        def worker():
            try:
                dynamic_pulses = self._build_bp_target_pulses(xyz_cm)
                execute_fixed_grasp_sequence(
                    arm_controller=self.arm,
                    fixed_pulses=dynamic_pulses,
                    gripper_angle=self.GRIPPER_ANGLE,
                    close_pulse=self._last_close_pulse,
                    home_pulses=self.HOME_PULSES,
                    home_open_pulses=self.HOME_OPEN_PULSES,
                    wait1_pulses=self.WAIT1_PULSES,
                    finish_pulses=self.FINISH_PULSES,
                    step_time_ms=self.GRIPPER_STEP_TIME_MS,
                )
                if self.auto_demo_mode and not self._return_to_menu_requested:
                    self._backup_two_steps('post-grasp')
            except Exception as e:
                print(f"[arm] {e}")
            finally:
                with self._lock:
                    self._grab_active = False
                    self._target_cm = None
                    self._last_prompt_t = time.time()

        with self._lock:
            if self._grab_active:
                return
            self._grab_active = True
        threading.Thread(target=worker, daemon=True).start()

    def _prompt_auto_align_async(self, cand):
        def worker():
            try:
                ans = input(
                    f"Best fruit selected: x={cand['x_cm']:.1f}cm, y={cand['y_cm']:.1f}cm, z={cand['z_cm']:.1f}cm, score={cand['score']:.3f}. "
                    f"Start automatic matching calibration? (y/n, {self.MENU_RETURN_CHAR}=menu): "
                ).strip().lower()
                if ans == self.MENU_RETURN_CHAR:
                    self._request_return_to_menu('user typed menu char during auto-align confirmation')
                elif ans in ('y', 'yes'):
                    with self._lock:
                        self._auto_align_active = True
                        self._align_start_t = time.time()
                        self._aligned_frames = 0
                        self._active_target = dict(cand)
                        self._active_target_last_seen_t = time.time()
                    print("[auto-align] started")
                else:
                    print("[auto-align] skipped by user")
            finally:
                with self._lock:
                    self._align_prompt_active = False
                    self._last_prompt_t = time.time()
        with self._lock:
            if self._align_prompt_active or self._auto_align_active or self._grab_active or self._prompt_active or self._timeout_prompt_active:
                return
            self._align_prompt_active = True
        threading.Thread(target=worker, daemon=True).start()

    def _handle_auto_align(self, current_target, frame_center_x):
        now = time.time()
        if current_target is None:
            if self._auto_align_active and (now - self._active_target_last_seen_t) > self.TARGET_LOST_TIMEOUT_S:
                print("[auto-align] target lost, stopping base")
                self._stop_base()
                with self._lock:
                    self._auto_align_active = False
                    self._active_target = None
                    self._aligned_frames = 0
            return
        with self._lock:
            self._active_target = dict(current_target)
            self._active_target_last_seen_t = now
        if self._align_start_t is not None and (now - self._align_start_t) > self.AUTO_ALIGN_TIMEOUT_S:
            print("[auto-align] timeout, stopping base")
            self._stop_base()
            xyz_cm = (current_target['x_cm'], current_target['y_cm'], current_target['z_cm'])
            with self._lock:
                self._auto_align_active = False
                self._active_target = dict(current_target)
                self._aligned_frames = 0
                self._target_cm = tuple(xyz_cm)
            if self.auto_demo_mode:
                print('[auto-demo] timeout -> direct BP grasp automatically once...')
                self._grab_sequence_async(tuple(xyz_cm))
            else:
                self._prompt_timeout_action_async(tuple(xyz_cm))
            return

        err_x_cm = current_target['x_cm'] - self.DESIRED_X_CM
        err_y_cm = current_target['y_cm'] - self.DESIRED_Y_CM
        err_px = current_target['pixel_x'] - frame_center_x

        if abs(err_px) > self.TURN_STAGE_PX:
            wz = -self.TURN_STAGE_WZ if err_px > 0 else self.TURN_STAGE_WZ
            vx = self.TURN_STAGE_VX
            vy = 0.0
            stage = "TURN"
        else:
            wz = -self._interp_speed(err_px, self.PIXEL_DEADBAND_PX, self.MAX_PIXEL_ERR_PX, self.MIN_WZ, self.MAX_WZ)
            vx = self._interp_speed(err_x_cm, self.X_DEADBAND_CM, self.MAX_FORWARD_ERR_CM, self.MIN_VX, self.MAX_VX)
            vy = 0.0
            stage = "APPROACH"

        self._publish_base_cmd(vx=vx, vy=vy, wz=wz)
        print(f"[align-{stage}] err_px={err_px:.1f}, err_x={err_x_cm:.1f}, err_y={err_y_cm:.1f}, vx={vx:.3f}, vy={vy:.3f}, wz={wz:.3f}")

        aligned = (abs(err_x_cm) <= self.SUCCESS_X_CM and abs(err_y_cm) <= self.SUCCESS_Y_CM and abs(err_px) <= self.SUCCESS_PIXEL_PX)
        if aligned:
            self._aligned_frames += 1
        else:
            self._aligned_frames = 0
        if self._aligned_frames >= self.ALIGN_SUCCESS_FRAMES:
            self._stop_base()
            print(f"[auto-align] success: x={current_target['x_cm']:.2f}cm, y={current_target['y_cm']:.2f}cm, z={current_target['z_cm']:.2f}cm, pixel=({current_target['pixel_x']}, {current_target['pixel_y']})")
            xyz_cm = (current_target['x_cm'], current_target['y_cm'], current_target['z_cm'])
            with self._lock:
                self._auto_align_active = False
                self._active_target = None
                self._aligned_frames = 0
                self._last_prompt_t = time.time()
                self._target_cm = tuple(xyz_cm)
            if self.auto_demo_mode:
                print('[auto-demo] aligned successfully, start BP grasp automatically...')
                self._grab_sequence_async(tuple(xyz_cm))
            else:
                print("[auto-align] asking for grab confirmation now...")
                self._prompt_grab_async(tuple(xyz_cm))

    def run(self):
        try:
            if self.display:
                cv2.namedWindow("RealSense YOLOv8 + Go2 Auto Align + Arm Grasp", cv2.WINDOW_NORMAL)
                cv2.resizeWindow("RealSense YOLOv8 + Go2 Auto Align + Arm Grasp", 1280, 720)
            while True:
                if self._should_return_to_menu():
                    return 'menu'

                frames = self.pipeline.wait_for_frames()
                aligned_frames = self.align.process(frames)
                color_frame = aligned_frames.get_color_frame()
                depth_frame = aligned_frames.get_depth_frame()
                if not color_frame or not depth_frame:
                    continue
                color_image = np.asanyarray(color_frame.get_data())
                results = self._predict(color_image)
                if not results:
                    continue
                result = results[0]
                annotated_frame = result.plot()
                frame_h, frame_w = annotated_frame.shape[:2]
                frame_center_x = frame_w // 2
                frame_center_y = frame_h // 2
                cv2.line(annotated_frame, (frame_center_x, 0), (frame_center_x, frame_h - 1), (0, 255, 255), 1)
                cv2.line(annotated_frame, (0, frame_center_y), (frame_w - 1, frame_center_y), (0, 255, 255), 1)
                cv2.circle(annotated_frame, (frame_center_x, frame_center_y), int(self.PIXEL_DEADBAND_PX), (0, 255, 255), 1)

                candidates = []
                for box in result.boxes:
                    x1, y1, x2, y2 = box.xyxy[0]
                    pixel_x = int((x1 + x2) / 2)
                    pixel_y = int((y1 + y2) / 2)
                    real_xyz = self.pixel_to_3d_xyz(depth_frame, pixel_x, pixel_y)
                    if real_xyz is None:
                        continue
                    xx, yy, zz = real_xyz
                    real_x = zz - 0.315
                    real_y = -xx + 0.04
                    real_z = -yy + 0.18
                    x_cm = real_x * self.M_TO_CM
                    y_cm = real_y * self.M_TO_CM
                    z_cm = real_z * self.M_TO_CM
                    conf = float(box.conf[0]) if hasattr(box, 'conf') and box.conf is not None else 0.0
                    if not self._is_candidate_valid(x_cm, y_cm, z_cm):
                        continue
                    candidate = {'box': (float(x1), float(y1), float(x2), float(y2)), 'pixel_x': pixel_x, 'pixel_y': pixel_y, 'x_cm': x_cm, 'y_cm': y_cm, 'z_cm': z_cm, 'conf': conf}
                    candidates.append(candidate)
                    cv2.putText(annotated_frame, f"pixel=({pixel_x},{pixel_y})", (int(x1), int(y1) - 34), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)
                    cv2.putText(annotated_frame, f"XYZ=({x_cm:.1f},{y_cm:.1f},{z_cm:.1f})cm", (int(x1), int(y1) - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

                best = self._select_best_candidate(candidates, frame_w, frame_h)
                current_target = best
                if self._auto_align_active:
                    locked = self._select_locked_candidate(candidates)
                    if locked is not None:
                        current_target = locked
                if best is not None:
                    bx1, by1, bx2, by2 = best['box']
                    cv2.rectangle(annotated_frame, (int(bx1), int(by1)), (int(bx2), int(by2)), (0, 255, 255), 2)
                    cv2.putText(annotated_frame, f"BEST score={best['score']:.3f}", (int(bx1), int(by2) + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                if current_target is not None:
                    tx1, ty1, tx2, ty2 = current_target['box']
                    cv2.rectangle(annotated_frame, (int(tx1), int(ty1)), (int(tx2), int(ty2)), (255, 255, 0), 2)
                    cv2.putText(annotated_frame, "TRACK", (int(tx1), int(ty2) + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                    err_x_cm = current_target['x_cm'] - self.DESIRED_X_CM
                    err_y_cm = current_target['y_cm'] - self.DESIRED_Y_CM
                    err_px = current_target['pixel_x'] - frame_center_x
                    cv2.putText(annotated_frame, f"err_x={err_x_cm:+.1f}cm err_y={err_y_cm:+.1f}cm err_px={err_px:+d}", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

                now = time.time()
                if not self._auto_align_active and not self._grab_active and not self._prompt_active and not self._timeout_prompt_active:
                    if best is None:
                        if self._no_fruit_since is None:
                            self._no_fruit_since = now
                        elif self.auto_demo_mode and (now - self._no_fruit_since) >= self.AUTO_DEMO_NO_FRUIT_TIMEOUT_S:
                            print(f"[auto-demo] no valid fruit detected for {self.AUTO_DEMO_NO_FRUIT_TIMEOUT_S:.1f}s. Auto demo completed.")
                            return 'done'
                    else:
                        self._no_fruit_since = None

                if best is not None and not self._auto_align_active and not self._align_prompt_active and not self._grab_active and not self._prompt_active and not self._timeout_prompt_active and (now - self._last_prompt_t) >= self.AUTO_PROMPT_COOLDOWN_S:
                    if self.auto_demo_mode:
                        with self._lock:
                            self._auto_align_active = True
                            self._align_start_t = time.time()
                            self._aligned_frames = 0
                            self._active_target = dict(best)
                            self._active_target_last_seen_t = time.time()
                        print(f"[auto-demo] auto-start align for best fruit: x={best['x_cm']:.1f}cm, y={best['y_cm']:.1f}cm, z={best['z_cm']:.1f}cm, score={best['score']:.3f}")
                    else:
                        self._prompt_auto_align_async(best)
                if self._auto_align_active:
                    self._handle_auto_align(current_target, frame_center_x)
                else:
                    if not self._grab_active and not self._timeout_prompt_active and not self._backing_up:
                        self._stop_base()

                curr_time = time.time()
                fps = 1.0 / max(curr_time - self.prev_time, 1e-6)
                self.prev_time = curr_time
                status = "IDLE"
                if self._align_prompt_active:
                    status = "WAIT_USER_START"
                elif self._auto_align_active:
                    status = "AUTO_ALIGNING"
                elif self._grab_active:
                    status = "GRABBING"
                elif self._prompt_active:
                    status = "WAIT_GRAB_CONFIRM"
                elif self._timeout_prompt_active:
                    status = "WAIT_TIMEOUT_DECISION"
                if self.auto_demo_mode:
                    status = f"AUTO_DEMO/{status}"
                cv2.putText(annotated_frame, f"FPS: {int(fps)} | STATUS: {status}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
                cv2.putText(annotated_frame, f"Desired grasp ~= ({self.DESIRED_X_CM:.0f}, {self.DESIRED_Y_CM:.0f}, {self.DESIRED_Z_CM:.0f}) cm", (10, frame_h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                mode_hint = "Mode4 auto demo" if self.auto_demo_mode else "Mode3 control"
                cv2.putText(annotated_frame, f"{mode_hint}: press 'm' in window or type '{self.MENU_RETURN_CHAR}' in prompts to return to mode menu", (10, frame_h - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
                if self.display:
                    key = cv2.waitKey(1) & 0xFF
                    cv2.imshow("RealSense YOLOv8 + Go2 Auto Align + Arm Grasp", annotated_frame)
                    if key == ord('q'):
                        break
                    if key == self.WINDOW_MENU_KEY:
                        if self._align_prompt_active or self._prompt_active:
                            print(f"[mode3] a terminal prompt is active. Type '{self.MENU_RETURN_CHAR}' in that prompt to return to the mode menu.")
                        else:
                            self._request_return_to_menu("window hotkey")
                if self._should_return_to_menu():
                    return 'menu'
        finally:
            try:
                self._stop_base()
            except Exception:
                pass
            self.pipeline.stop()
            if self.display:
                cv2.destroyAllWindows()
            self.close()


def choose_test_mode():
    print("\nSelect execution mode:")
    print("  1. 读取位置 (record current servo parameters)")
    print("  2. 仅执行抓取 (fixed-position grasp only)")
    print("  3. 校对与抓取 (visual alignment + BP grasp)")
    print("  4. 自动展示抓取 (auto demo: align + BP grasp loop)")
    print("  q. 退出")
    raw = input("Enter 1/2/3/4/q: ").strip().lower()
    if raw in ('1', 'read', 'record', '读取位置'):
        return 'record'
    if raw in ('2', 'grasp', 'only', '仅执行抓取'):
        return 'grasp_only'
    if raw in ('3', 'align', 'align_and_grasp', '校对与抓取'):
        return 'align_and_grasp'
    if raw in ('4', 'auto', 'demo', 'auto_demo', '自动展示抓取'):
        return 'auto_demo'
    if raw in ('q', 'quit', 'exit', '退出'):
        return 'quit'
    return 'align_and_grasp'


if __name__ == "__main__":
    while True:
        mode = choose_test_mode()
        if mode == 'quit':
            break
        if mode == 'record':
            run_record_fixed_pose_mode(arm_port='/dev/ttyUSB0', arm_baudrate=9600)
            continue
        if mode == 'grasp_only':
            run_fixed_grasp_only_mode(arm_port='/dev/ttyUSB0', arm_baudrate=9600)
            continue

        detector = RealSenseYOLOWithDepth(
            model_path='bestn.engine',
            arm_port='/dev/ttyUSB0',
            arm_baudrate=9600,
            imgsz=640,
            conf=0.25,
            display=True,
            go2_iface='eth0',
            bp_model_path='bp_model.pth',
            fixed_grasp_pulses=get_recorded_fixed_grasp_pulses(),
            auto_demo_mode=(mode == 'auto_demo'),
        )
        result = detector.run()
        if result == 'menu':
            continue
        if result == 'done':
            print('[mode4] auto demo finished: no valid fruit remaining.')
            continue
        break
