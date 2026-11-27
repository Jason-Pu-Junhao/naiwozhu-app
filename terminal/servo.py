# -*- coding: utf-8 -*-
"""
servo.py —— 唤醒时的物理反馈：让舵机动一下

两种舵机都支持（`config.SERVO_MODE` 切换）：

  position    位置舵机（SG90 / MG90S 180° 版）
              真正按角度走：0° → +30° → -30° → 0°，速度 60°/s。
              这是最初想要的效果。

  continuous  连续旋转舵机（MG90S 360° 版）
              这种舵机没有角度概念：1.5ms 停、<1.5ms 正转、>1.5ms 反转，
              转速随偏差变化。所以做不了 ±30° 摆动，改成「点两下」：
              正转 0.15s → 停 → 反转 0.15s → 停。
              看起来像抖一下，是有物理反馈的。

为什么两种都支持：
  实测手上这颗 MG90S 是 360° 版——发 1500us 停、1000us 正转不停、2000us 反转不停，
  根本停不到某个角度。写死位置模式的话它只会一个方向狂转。

PWM 优先级：
  1. lgpio 直接输出硬件 PWM（精确，无抖动）
  2. gpiozero（软件 PWM，抖动 ±0.5°，够用但不精确）
  3. 都不可用 → 打印一行提示，主程序照常跑

相关文件：被 assistant.py 的 ack() 调用；参数在 config.py。
"""
import threading
import time

import config

_lock = threading.Lock()
_busy_event = None
_kind = None             # "lgpio" / "gpiozero"
_handle = None           # lgpio 的 chip handle，或 gpiozero 的 Servo 对象
_init_tried = False
_GPIO = None


def _init():
    """打开舵机。lgpio 优先（硬件 PWM），失败退 gpiozero。"""
    global _kind, _handle, _init_tried, _GPIO
    if _kind is not None or _init_tried:
        return _kind is not None
    _init_tried = True
    _GPIO = config.SERVO_PIN

    try:
        import lgpio

        _handle = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(_handle, _GPIO)
        lgpio.tx_pwm(_handle, _GPIO, 50.0, 7.5)    # 先给中位，让舵机停住
        _kind = "lgpio"
        print(f"[servo] 就绪（GPIO{_GPIO}，lgpio 硬件 PWM，{config.SERVO_MODE} 模式）",
              flush=True)
        return True
    except Exception as e:
        print(f"[servo] lgpio 不可用（{type(e).__name__}），尝试 gpiozero", flush=True)
        _handle = None

    try:
        from gpiozero import Servo

        _handle = Servo(_GPIO)
        _handle.mid()
        _kind = "gpiozero"
        print(f"[servo] 就绪（GPIO{_GPIO}，gpiozero 软件 PWM，{config.SERVO_MODE} 模式）",
              flush=True)
        return True
    except Exception as e:
        print(f"[servo] 不可用，已跳过：{type(e).__name__}: {e}", flush=True)
        _kind = None
        _handle = None
        return False


def _pulse(us):
    """输出脉宽 (微秒)，50Hz。这是两种舵机唯一的共同语言。"""
    if _kind is None:
        return
    if _kind == "lgpio":
        import lgpio

        duty = us / 20000.0 * 100.0                # 50Hz → 周期 20000us
        lgpio.tx_pwm(_handle, _GPIO, 50.0, duty)
    else:
        # gpiozero: value -1 → 0.5ms, 0 → 1.5ms, +1 → 2.5ms
        _handle.value = max(-1.0, min(1.0, (us - 1500.0) / 1000.0))


def _stop_pwm():
    """彻底停掉 PWM 输出（否则舵机会一直保持力矩、发热）。"""
    if _kind is None:
        return
    if _kind == "lgpio":
        import lgpio

        lgpio.tx_pwm(_handle, _GPIO, 0, 0)
    else:
        _handle.value = None


def _angle_to_us(deg):
    """角度 → 脉宽。±90° 对应 1000/2000us，线性映射。"""
    return 1500.0 + (deg / 90.0) * 500.0


def _swing_position():
    """位置舵机：0° → +30° → -30° → 0°，速度 60°/s，全程约 1.5 秒。"""
    step_sec = config.SERVO_SWING_DEG / config.SERVO_DEG_PER_SEC     # 30/60 = 0.5s
    _pulse(_angle_to_us(0))
    time.sleep(0.05)
    _pulse(_angle_to_us(config.SERVO_SWING_DEG))
    time.sleep(step_sec)
    _pulse(_angle_to_us(-config.SERVO_SWING_DEG))
    time.sleep(step_sec)
    _pulse(_angle_to_us(0))
    time.sleep(0.05)


def _swing_continuous():
    """连续旋转舵机：正转一下 → 停 → 反转一下 → 停，看起来像「抖两下」。

    没有角度反馈，所以只能靠时长控制转速×时间。0.15 秒大约转 30~50°，
    具体取决于舵机的实际转速（不同克隆件差异很大）。
    """
    t = config.SERVO_SPIN_SEC
    off = config.SERVO_SPIN_US
    _pulse(1500)                     # 停
    time.sleep(0.05)
    _pulse(1500 - off)               # 正转
    time.sleep(t)
    _pulse(1500)                     # 停
    time.sleep(0.08)
    _pulse(1500 + off)               # 反转
    time.sleep(t)
    _pulse(1500)                     # 停
    time.sleep(0.05)


def _swing():
    mode = config.SERVO_MODE
    try:
        if mode == "continuous":
            _swing_continuous()
        else:
            _swing_position()
    except Exception as e:
        print(f"[servo] 摆动失败：{type(e).__name__}: {e}", flush=True)


def nod():
    """非阻塞地动一下。唤醒时调它，立刻返回，不影响录音。"""
    if not config.SERVO:
        return
    if not _init():
        return
    global _busy_event
    if _busy_event is not None and _busy_event.is_set():
        return                                    # 上一趟没走完，跳过
    _busy_event = threading.Event()
    _busy_event.set()

    def run():
        try:
            with _lock:
                _swing()
        finally:
            _busy_event.clear()

    threading.Thread(target=run, daemon=True, name="servo-nod").start()


def release():
    """退出前停 PWM 并释放，避免舵机一直有劲、发热。"""
    global _kind, _handle
    if _kind is None:
        return
    try:
        _stop_pwm()
        if _kind == "lgpio":
            import lgpio

            lgpio.gpiochip_close(_handle)
        else:
            _handle.close()
    except Exception:
        pass
    _kind = None
    _handle = None


def selftest():
    """自检：把当前模式的动作走一遍。"""
    print(f"[servo] SERVO={config.SERVO} PIN={config.SERVO_PIN} "
          f"模式={config.SERVO_MODE}", flush=True)
    if not _init():
        print("[servo] 没有可用的舵机", flush=True)
        return False

    if config.SERVO_MODE == "continuous":
        print(f"[servo] 正转 {config.SERVO_SPIN_SEC}s → 停 → 反转 {config.SERVO_SPIN_SEC}s → 停",
              flush=True)
    else:
        print(f"[servo] 幅度 ±{config.SERVO_SWING_DEG}° 速度 {config.SERVO_DEG_PER_SEC}°/s",
              flush=True)
    for _ in range(2):
        _swing()
        time.sleep(0.4)
    print("[servo] 自检完成", flush=True)
    return True
