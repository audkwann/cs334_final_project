import math
import time
import random
import sys
import threading
import requests

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    print("Missing dependency: pyserial. Install with: pip install pyserial")
    sys.exit(1)

REACHY_API = "http://localhost:8000"

# Encoder constants (must match pulley.ino)
TICKS_PER_REV = 16567
NUM_SEGMENTS = 30           # 30 equal parts = 12 degrees each

# Reachy body yaw limits (radians). Adjust if Reachy can turn further.
MAX_BODY_YAW_RAD = 1.13     # ~65 degrees each direction

# Viewing positions — where Reachy looks to inspect items
REACHY_VIEW_RIGHT_DEG = 65   # Reachy looks right to this table degree
REACHY_VIEW_LEFT_DEG = 295   # Reachy looks left to this table degree

# Emotions
idle_emotions = [
    "curious1", "inquiring1", "inquiring2", "attentive1", "attentive2",
    "thoughtful1", "thoughtful2", "serenity1", "calming1", "welcoming1",
    "understanding1", "helpful1", "shy1", "relief1",
]
thinking_emotions = [
    "thoughtful1", "thoughtful2", "curious1", "inquiring1", "inquiring2",
    "uncertain1", "confused1", "attentive1", "attentive2",
]
decided_emotions = [
    "understanding1", "calming1", "serenity1", "relief1", "helpful1",
]

# --- Reachy API helpers ---

def check_api_health():
    try:
        requests.get(f"{REACHY_API}/", timeout=3)
        print("Reachy API: connected")
        return True
    except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout, requests.exceptions.Timeout):
        print("Reachy API: not reachable")
        return False

reachy_available = check_api_health()

def play_emotion(emotion_name):
    if not reachy_available:
        return
    dataset_name = "pollen-robotics%2Freachy-mini-emotions-library"
    url = f"{REACHY_API}/api/move/play/recorded-move-dataset/{dataset_name}/{emotion_name}"
    try:
        requests.post(url, timeout=10)
    except requests.exceptions.RequestException:
        pass

def reachy_goto(body_yaw_rad=None, head_yaw_rad=None, head_pitch_rad=None, duration=2.0, interpolation="minjerk"):
    """Move Reachy's body yaw and/or head pose via the goto API. Angles in radians."""
    if not reachy_available:
        return
    payload = {"duration": duration, "interpolation": interpolation}
    if body_yaw_rad is not None:
        payload["body_yaw"] = body_yaw_rad
    if head_yaw_rad is not None or head_pitch_rad is not None:
        payload["head_pose"] = {
            "x": 0, "y": 0, "z": 0, "roll": 0,
            "pitch": head_pitch_rad if head_pitch_rad is not None else 0.0,
            "yaw": head_yaw_rad if head_yaw_rad is not None else 0.0,
        }
    try:
        requests.post(f"{REACHY_API}/api/move/goto", json=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"  [reachy] goto error: {e}")

def get_body_yaw():
    """Get current body yaw in radians."""
    try:
        r = requests.get(f"{REACHY_API}/api/state/present_body_yaw", timeout=3)
        return float(r.text)
    except Exception:
        return 0.0

# --- Serial / Arduino setup ---

def find_arduino_port():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    for p in ports:
        if p.device.startswith(("/dev/tty.usb", "/dev/cu.usb")) or "arduino" in (p.description or "").lower():
            return p.device
    print("No Arduino found.")
    return None

arduino_port = find_arduino_port()
ser = None

_encoder_position = 0
_encoder_lock = threading.Lock()
_serial_stop = threading.Event()

if arduino_port:
    try:
        ser = serial.Serial(arduino_port, 115200, timeout=0.25)
        print(f"Opened {arduino_port} — waiting for Arduino reset...")
        time.sleep(2.0)

        def _serial_reader():
            global _encoder_position
            while not _serial_stop.is_set():
                try:
                    line = ser.readline()
                except serial.SerialException:
                    return
                if line:
                    msg = line.decode("utf-8", errors="replace").strip()
                    if msg:
                        if msg.startswith("POS "):
                            try:
                                with _encoder_lock:
                                    _encoder_position = int(msg[4:])
                            except ValueError:
                                pass
                        elif msg.startswith("REACHED"):
                            print(f"  [motor] Reached target")
                        elif msg.startswith("ZEROED"):
                            with _encoder_lock:
                                _encoder_position = 0
                        elif not msg.startswith("POS"):
                            print(f"  [motor] {msg}")
        threading.Thread(target=_serial_reader, daemon=True).start()
    except serial.SerialException as exc:
        print(f"Failed to open {arduino_port}: {exc}")
        ser = None
else:
    print("Motor control disabled (no Arduino found).")

def send_serial(cmd):
    if ser and ser.is_open:
        ser.write(f"{cmd}\n".encode("ascii"))
        ser.flush()

def get_position():
    with _encoder_lock:
        return _encoder_position

def goto_ticks(ticks):
    send_serial(f"g{ticks}")

def degrees_to_ticks(degrees):
    return int((degrees / 360.0) * TICKS_PER_REV)

def degrees_to_yaw_rad(degrees):
    """Map a table degree (0-360, clockwise) to Reachy body yaw (radians).
    Clamps to Reachy's physical range."""
    # Table degrees: 0=front, increases clockwise when viewed from above
    # Reachy yaw: 0=forward, positive=left, negative=right
    # Map: table 0-180 -> reachy negative yaw (look right), 180-360 -> positive yaw (look left)
    # Normalize to -180..180
    angle = degrees % 360
    if angle > 180:
        angle -= 360
    yaw_rad = math.radians(-angle)  # negate: table clockwise = reachy looks right (negative yaw)
    return max(-MAX_BODY_YAW_RAD, min(MAX_BODY_YAW_RAD, yaw_rad))

# --- Idle emotion loop ---

_idle_stop = threading.Event()
_idle_paused = threading.Event()

def _idle_loop():
    while not _idle_stop.is_set():
        # Idle emotions disabled for testing
        _idle_stop.wait(10.0)

threading.Thread(target=_idle_loop, daemon=True).start()

# --- Fetch sequence ---

def wait_for_table(rotation_degrees):
    """Estimate wait time for the table to rotate a given number of degrees."""
    return max(2.0, (abs(rotation_degrees) / 360.0) * 8.0) + 1.0

def fetch_item(degrees):
    """Pre-rotate table to viewing position, look at item, think, then bring to 0."""
    _idle_paused.set()

    try:
        degrees = degrees % 360

        # 0-180: spin table right (positive ticks), Reachy looks right at 65 deg
        # 180-360: spin table left (negative ticks), Reachy looks left at 295 deg
        if degrees <= 180:
            view_deg = REACHY_VIEW_RIGHT_DEG
            pre_rotate_deg = degrees - view_deg
            final_deg = degrees
        else:
            view_deg = REACHY_VIEW_LEFT_DEG
            pre_rotate_deg = -(360 - degrees) + (360 - view_deg)
            final_deg = -(360 - degrees)

        pre_rotate_ticks = degrees_to_ticks(pre_rotate_deg)
        final_ticks = degrees_to_ticks(final_deg)
        view_yaw = degrees_to_yaw_rad(view_deg)

        # Step 1: Pre-rotate table so item lands at the viewing position
        print(f"\n  Step 1: Rotating table to bring {degrees:.0f} deg item to {view_deg} deg viewing position...")
        goto_ticks(pre_rotate_ticks)
        pre_wait = wait_for_table(pre_rotate_deg)
        time.sleep(pre_wait)

        # Step 2: Reachy looks at the viewing position
        print(f"  Step 2: Looking at {view_deg} deg...")
        reachy_goto(body_yaw_rad=view_yaw, head_yaw_rad=view_yaw * 0.8, head_pitch_rad=0.45, duration=2.0, interpolation="minjerk")
        time.sleep(2.5)

        # Step 3: Thinking — gentle body wobble while looking at the item
        print(f"  Step 3: Thinking...")
        think_start = time.time()
        think_duration = 4.0

        while True:
            elapsed = time.time() - think_start
            if elapsed >= think_duration:
                break

            wobble = random.uniform(-0.1, 0.1)
            wobble_body = max(-MAX_BODY_YAW_RAD, min(MAX_BODY_YAW_RAD, view_yaw + wobble))
            wobble_head = (view_yaw + wobble) * 0.8
            reachy_goto(body_yaw_rad=wobble_body, head_yaw_rad=wobble_head, head_pitch_rad=0.45, duration=1.5, interpolation="minjerk")
            time.sleep(2.0)

        # Step 4: Rotate table the rest of the way to bring item to 0,
        # and Reachy returns to center concurrently
        print(f"  Step 4: Fetching item to position 0...")
        goto_ticks(final_ticks)

        return_duration = max(2.0, (65.0 / 360.0) * 8.0)
        reachy_goto(body_yaw_rad=0.0, head_yaw_rad=0.0, head_pitch_rad=-0.05, duration=return_duration, interpolation="minjerk")

        time.sleep(return_duration + 1.0)

        print(f"  Done! Item from {degrees:.0f} deg is now at position 0.\n")

    finally:
        _idle_paused.clear()

# --- Main input loop ---

def print_help():
    print()
    print("=== Fetch Item ===")
    print("  Enter a degree (0-360) or segment (s0-s29) to fetch that item to position 0.")
    print("  Reachy will look at it, think, then rotate it to the front.")
    print()
    print("  Commands:")
    print("    <degrees>    Fetch item at that degree (e.g. '90')")
    print("    s<segment>   Fetch item at segment 0-29 (e.g. 's5' = 60 degrees)")
    print("    z            Zero encoder at current position")
    print("    p            Print current position")
    print("    home         Return table to position 0")
    print("    stop         Stop motor")
    print("    h            Show this help")
    print("    q            Quit")
    print()

print_help()

try:
    while True:
        try:
            raw = input(">> ").strip().lower()
        except EOFError:
            break

        if not raw:
            continue

        if raw == "q":
            break
        elif raw == "h":
            print_help()
        elif raw == "z":
            send_serial("z")
            print("  Encoder zeroed (home set)")
        elif raw == "p":
            pos = get_position()
            deg = (pos / TICKS_PER_REV) * 360.0
            print(f"  Position: {pos} ticks | {deg:.1f} degrees")
        elif raw == "stop":
            send_serial("3")
            print("  Motor stopped")
        elif raw == "home":
            goto_ticks(0)
            print("  Returning to position 0...")
        elif raw.startswith("s"):
            try:
                seg = int(raw[1:])
                if 0 <= seg < NUM_SEGMENTS:
                    deg = seg * (360.0 / NUM_SEGMENTS)
                    print(f"  Segment {seg} = {deg:.0f} degrees")
                    fetch_item(deg)
                else:
                    print(f"  Segment must be 0-{NUM_SEGMENTS - 1}")
            except ValueError:
                print("  Usage: s<number> (e.g. 's5')")
        else:
            try:
                deg = float(raw)
                if 0 <= deg <= 360:
                    fetch_item(deg)
                else:
                    print("  Degrees must be 0-360")
            except ValueError:
                print("  Unknown command. Type 'h' for help.")

except KeyboardInterrupt:
    print("\nStopping...")
finally:
    _idle_stop.set()
    _idle_paused.set()
    if reachy_available:
        reachy_goto(body_yaw_rad=0.0, head_yaw_rad=0.0, head_pitch_rad=0.0, duration=1.5)
    if ser and ser.is_open:
        send_serial("3")
        _serial_stop.set()
        ser.close()
    print("Done.")
