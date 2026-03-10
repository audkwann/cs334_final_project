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
TICKS_PER_SEGMENT = TICKS_PER_REV // NUM_SEGMENTS

# All available emotions
idle_emotions = [
    "curious1", "inquiring1", "inquiring2", "inquiring3", "attentive1", "attentive2",
    "thoughtful1", "thoughtful2", "serenity1", "calming1", "understanding1",
    "understanding2", "helpful1", "helpful2", "cheerful1", "shy1", "uncertain1",
    "loving1", "welcoming1", "welcoming2", "relief1", "relief2",
]

# --- Reachy API ---

def play_emotion(emotion_name):
    if not reachy_available:
        return
    dataset_name = "pollen-robotics%2Freachy-mini-emotions-library"
    url = f"{REACHY_API}/api/move/play/recorded-move-dataset/{dataset_name}/{emotion_name}"
    try:
        response = requests.post(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        pass

def check_api_health():
    try:
        requests.get(f"{REACHY_API}/", timeout=3)
        print("Reachy API: connected")
        return True
    except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout, requests.exceptions.Timeout):
        print("Reachy API: not reachable — idle emotions disabled")
        return False

reachy_available = check_api_health()

# --- Serial / Arduino setup ---

def find_arduino_port():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    for p in ports:
        if p.device.startswith(("/dev/tty.usb", "/dev/cu.usb")) or "arduino" in (p.description or "").lower():
            return p.device
    print("No Arduino found. Available ports:")
    for p in ports:
        print(f"  {p.device} - {p.description or 'Unknown'}")
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
                        elif msg.startswith("REACHED "):
                            print(f"  [motor] {msg}")
                        elif msg.startswith("ZEROED"):
                            with _encoder_lock:
                                _encoder_position = 0
                            print(f"  [motor] Zeroed")
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

def goto_degrees(degrees):
    degrees = degrees % 360
    ticks = int((degrees / 360.0) * TICKS_PER_REV)
    print(f"  Going to {degrees:.1f} degrees (ticks={ticks})")
    goto_ticks(ticks)

def goto_segment(segment):
    segment = segment % NUM_SEGMENTS
    degrees = segment * (360.0 / NUM_SEGMENTS)
    goto_degrees(degrees)

# --- Idle emotion loop ---

_idle_stop = threading.Event()

def _idle_loop():
    """Play a random idle emotion every 4-8 seconds so Reachy never stares blankly."""
    while not _idle_stop.is_set():
        if reachy_available:
            emotion = random.choice(idle_emotions)
            play_emotion(emotion)
        wait = random.uniform(4.0, 8.0)
        _idle_stop.wait(wait)

threading.Thread(target=_idle_loop, daemon=True).start()

# --- Main input loop ---

def print_help():
    print()
    print("=== Table Control ===")
    print(f"  30 segments, 12 degrees each")
    print()
    print("  Commands:")
    print("    <number>     Go to segment 0-29 (e.g. '5' = 60 degrees)")
    print("    d <degrees>  Go to exact degrees (e.g. 'd 90')")
    print("    z            Zero encoder at current position")
    print("    p            Print current position")
    print("    s            Stop motor")
    print("    h            Show this help")
    print("    q            Quit")
    print()
    print("  Segment map:")
    for i in range(NUM_SEGMENTS):
        deg = i * (360.0 / NUM_SEGMENTS)
        marker = " <-- home" if i == 0 else ""
        print(f"    {i:2d} = {deg:5.1f} degrees{marker}")
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
            seg = round(deg / (360.0 / NUM_SEGMENTS)) % NUM_SEGMENTS
            print(f"  Position: {pos} ticks | {deg:.1f} degrees | segment ~{seg}")
        elif raw == "s":
            send_serial("3")
            print("  Motor stopped")
        elif raw.startswith("d "):
            try:
                degrees = float(raw[2:])
                goto_degrees(degrees)
            except ValueError:
                print("  Usage: d <degrees>  (e.g. 'd 90')")
        else:
            try:
                segment = int(raw)
                if 0 <= segment < NUM_SEGMENTS:
                    deg = segment * (360.0 / NUM_SEGMENTS)
                    print(f"  Segment {segment} = {deg:.1f} degrees")
                    goto_segment(segment)
                else:
                    print(f"  Segment must be 0-{NUM_SEGMENTS - 1}")
            except ValueError:
                print("  Unknown command. Type 'h' for help.")

except KeyboardInterrupt:
    print("\nStopping...")
finally:
    _idle_stop.set()
    if ser and ser.is_open:
        send_serial("3")
        _serial_stop.set()
        ser.close()
    print("Done.")
