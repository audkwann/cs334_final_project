import cv2
import time
import requests
from PIL import Image
from transformers import BlipProcessor, BlipForConditionalGeneration
import subprocess
import random
import sys
import threading
try:
    from reachy_mini import ReachyMini
    from reachy_mini.utils import create_head_pose
    from reachy_mini.utils.interpolation import InterpolationTechnique
except ModuleNotFoundError:
    print("reachy_mini not installed — Reachy features disabled.")

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    print("Missing dependency: pyserial. Install it with `pip install pyserial`.")
    sys.exit(1)

"""
Reachy generates a caption and reacts when you press SPACE on the keyboard.
"""

REACHY_API = "http://localhost:8000"

# Encoder / position constants (must match pulley.ino)
TICKS_PER_REV = 16567
NUM_ZONES = 4  # 4 zones = 90-degree increments
TRIGGER_KEY = ord(" ")  # SPACE bar - press in the OpenCV window to capture and react

def play_emotion(emotion_name: str):
    if not reachy_available:
        print(f"[skip] Would play emotion: {emotion_name}")
        return
    dataset_name = "pollen-robotics%2Freachy-mini-emotions-library"
    url = f"{REACHY_API}/api/move/play/recorded-move-dataset/{dataset_name}/{emotion_name}"
    try:
        response = requests.post(url, timeout=10)
        response.raise_for_status()
        print(f"Playing emotion: {emotion_name} → {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Emotion API error: {e}")

def check_api_health():
    """Quick check that the Reachy API is reachable before starting"""
    try:
        r = requests.get(f"{REACHY_API}/", timeout=3)
        print("✓ Reachy API is reachable")
        return True
    except requests.exceptions.ConnectionError:
        print("✗ Cannot reach Reachy API at localhost:8000 — is the robot connected?")
        return False

# --- Setup ---
reachy_available = check_api_health()
if not reachy_available:
    print("Continuing without Reachy — motor control still available.")

# --- Serial motor setup ---
def find_arduino_port():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    for p in ports:
        if p.device.startswith(("/dev/tty.usb", "/dev/cu.usb")) or "arduino" in (p.description or "").lower():
            return p.device
    print("No Arduino serial port found. Available ports:")
    for p in ports:
        print(f"  {p.device} - {p.description or 'Unknown'}")
    return None

arduino_port = find_arduino_port()
ser = None

# Shared encoder position (updated by serial reader thread)
_encoder_position = 0
_encoder_lock = threading.Lock()
_serial_stop = threading.Event()

if arduino_port:
    try:
        ser = serial.Serial(arduino_port, 115200, timeout=0.25)
        print(f"Opened {arduino_port} — waiting for Arduino reset...")
        time.sleep(2.0)

        # Background thread to print Arduino responses and parse POS messages
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
                            print(f"[arduino] {msg}")
                        elif msg.startswith("ZEROED"):
                            with _encoder_lock:
                                _encoder_position = 0
                            print(f"[arduino] {msg}")
                        else:
                            print(f"[arduino] {msg}")
        threading.Thread(target=_serial_reader, daemon=True).start()
    except serial.SerialException as exc:
        print(f"Failed to open {arduino_port}: {exc}")
        ser = None
else:
    print("Motor control disabled (no Arduino found).")

def send_motor_command(cmd: str):
    """Send a single-char command ('1','2','3') to the Arduino."""
    if ser and ser.is_open:
        ser.write(f"{cmd}\n".encode("ascii"))
        ser.flush()
        labels = {"1": "LEFT", "2": "RIGHT", "3": "STOP"}
        print(f"Motor: sent {labels.get(cmd, cmd)}")
    else:
        print(f"Motor: no serial connection (would send '{cmd}')")

def get_encoder_position():
    """Return the latest encoder position from the Arduino."""
    with _encoder_lock:
        return _encoder_position

def send_goto_position(zone):
    """Send a go-to-position command for the given zone number (0-based)."""
    target_ticks = zone * (TICKS_PER_REV // NUM_ZONES)
    if ser and ser.is_open:
        cmd = f"g{target_ticks}\n"
        ser.write(cmd.encode("ascii"))
        ser.flush()
        print(f"Motor: goto zone {zone} (ticks={target_ticks})")
    else:
        print(f"Motor: no serial connection (would goto zone {zone}, ticks={target_ticks})")

cap = cv2.VideoCapture(0)

COOLDOWN = 2  # seconds between reactions (avoid repeat when key is held)
last_reaction_time = 0

processor = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
model = BlipForConditionalGeneration.from_pretrained("Salesforce/blip-image-captioning-base")

''' EMOTIONS:
    ["fear1","exhausted1","loving1","dance3","boredom2","relief1","anxiety1","disgusted1","welcoming1",
    "impatient1","sad1","helpful2","resigned1","amazed1","thoughtful2","lost1","surprised1","serenity1",
    "displeased1","incomprehensible2","irritated2","yes_sad1","dance2","understanding1","contempt1",
    "inquiring1","rage1","attentive2","no1","oops1","proud3","reprimand3","reprimand2","scared1",
    "no_excited1","come1","proud2","success1","enthusiastic2","laughing1","dying1","success2",
    "enthusiastic1","curious1","laughing2","tired1","reprimand1","proud1","grateful1","frustrated1",
    "calming1","attentive1","furious1","oops2","irritated1","yes1","confused1","understanding2","dance1",
    "shy1","inquiring2","uncertain1","thoughtful1","surprised2","displeased2","impatient2","welcoming2",
    "indifferent1","sad2","helpful1","lonely1","cheerful1","inquiring3","downcast1","sleep1","boredom1",
    "uncomfortable1","go_away1","electric1","relief2","no_sad1"]
    '''

happy_emotions = ["loving1", "dance3", "proud3", "proud2", "success1", "enthusiastic2", "success2", "enthusiastic1", 
                  "proud1", "grateful1", "yes1", "dance1", "welcoming2", "cheerful1"]
sad_emotions = ["boredom2", "sad1", "resigned1", "displeased1", "irritated2", 
                "rage1","no1","reprimand3","reprimand2","reprimand1", "frustrated1","irritated1",
                "displeased2","go_away1"]

# --- Emotion rule setup (chosen once per run) ---
TECH_KEYWORDS = ["phone", "mouse", "cell", "camera", "pink object", "blue object", "white box", "white square"]
PAPER_KEYWORDS = ["paper", "newspaper", "magazine", "book", "document", "check", "card"]
FOOD_KEYWORDS = ["apple", "orange", "lemon", "food", "fruit", "can", "soda", "bottle", "coca", "cola"]

EMOTION_RULES = ["likes_tech", "likes_paper", "likes_food"]

EMOTION_RULE = random.choice(EMOTION_RULES)
EMOTION_RULE_DESCRIPTIONS = {
    "likes_tech": "happy if the object is a tech device, otherwise sad",
    "likes_paper": "happy if the object is a paper-related device, otherwise sad",
    "likes_food": "happy if the object is a food item, otherwise sad",
}
EMOTION_RULE_DESCRIPTION = EMOTION_RULE_DESCRIPTIONS[EMOTION_RULE]

print(f"Using emotion rule for this run: {EMOTION_RULE_DESCRIPTION}")
print(f"Controls: SPACE=caption | 1/2/3=left/right/stop | 4-7=zone positions | p=position z=zero | y/n=mood | q=quit")


def decide_emotion_from_caption(caption: str):
    """Return (emotion_name, 'happy' | 'sad') based on the global EMOTION_RULE."""
    caption_lower = caption.lower()
    mood = "sad"

    if EMOTION_RULE == "likes_tech":
        mood = "happy" if any(word in caption_lower for word in TECH_KEYWORDS) else "sad"
    elif EMOTION_RULE == "likes_paper":
        mood = "happy" if any(word in caption_lower for word in PAPER_KEYWORDS) else "sad"
    elif EMOTION_RULE == "likes_food":
        mood = "happy" if any(word in caption_lower for word in FOOD_KEYWORDS) else "sad"

    if mood == "happy":
        return random.choice(happy_emotions), "happy"
    else:
        return random.choice(sad_emotions), "sad"

def react_with_mood(mood: str):
    """Make Reachy react with a happy or sad emotion and matching speech."""
    happy_sayings = ["Yay, I like this! Please place this object on the circle below.", "I want this! Please place this object on the circle below.", "I love this! Please place this object on the circle below."]
    sad_sayings = ["I do not like this.", "Don't show this to me again.", "Gross. Take this away.", "I hate this.", "I don't want this. Take it away.", "Why would you give me this? Take it away."]

    if mood == "happy":
        emotion_name = random.choice(happy_emotions)
        subprocess.run(["say", random.choice(happy_sayings)])
    else:
        emotion_name = random.choice(sad_emotions)
        subprocess.run(["say", "-v", "Ralph", random.choice(sad_sayings)])

    print(f"Manual trigger | Mood: {mood} | Emotion: {emotion_name}")
    play_emotion(emotion_name)

def generate_caption(frame, bbox=None):
    # If we have an object bounding box, crop to it so BLIP focuses on the object.
    img_frame = frame
    if bbox is not None:
        h, w = frame.shape[:2]
        start_x, start_y, end_x, end_y = bbox
        start_x = max(0, min(w - 1, int(start_x)))
        end_x = max(0, min(w, int(end_x)))
        start_y = max(0, min(h - 1, int(start_y)))
        end_y = max(0, min(h, int(end_y)))
        if end_x > start_x and end_y > start_y:
            img_frame = frame[start_y:end_y, start_x:end_x]

    img = Image.fromarray(cv2.cvtColor(img_frame, cv2.COLOR_BGR2RGB))
    inputs = processor(images=img, return_tensors="pt")
    out = model.generate(**inputs)
    caption = processor.decode(out[0], skip_special_tokens=True)

    print("Caption:", caption)

    emotion_name, mood = decide_emotion_from_caption(caption)
    print(f"Emotion rule: {EMOTION_RULE_DESCRIPTION} | Mood: {mood} | Emotion: {emotion_name}")

    happy_sayings = ["Yay, I like this! Please place this object on the circle below.", "I want this! Please place this object on the circle below.", "I love this! Please place this object on the circle below."]
    sad_sayings = ["I do not like this.", "Don't show this to me again.", "Gross. Take this away.", "I hate this.", "I don't want this. Take it away.", "Why would you give me this? Take it away."]

    if mood == "happy":
        subprocess.run(["say", random.choice(happy_sayings)])
    else:
        subprocess.run(["say", "-v", "Ralph", random.choice(sad_sayings)])

    play_emotion(emotion_name)


# --- Main loop ---
try:
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Show what Reachy sees
        display = frame.copy()
        status = "SPACE=caption | 1=left 2=right 3=stop | 4-7=zones | p=pos z=zero | y/n=mood | q=quit"
        cv2.putText(display, status, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(display, status, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)
        cv2.imshow("Reachy's view", display)

        key = cv2.waitKey(1) & 0xFF
        current_time = time.time()

        # Trigger on SPACE (TRIGGER_KEY) when the OpenCV window has focus
        if key == TRIGGER_KEY and (current_time - last_reaction_time > COOLDOWN):
            print("Key pressed - capturing frame and reacting...")
            generate_caption(frame, bbox=None)
            last_reaction_time = current_time
        
        # Trigger on 'y': manually force Reachy to be happy
        elif key == ord("y") and (current_time - last_reaction_time > COOLDOWN):
            print("'y' pressed - making Reachy happy...")
            react_with_mood("happy")
            last_reaction_time = current_time

        # Trigger on 'n': manually force Reachy to be sad
        elif key == ord("n") and (current_time - last_reaction_time > COOLDOWN):
            print("'n' pressed - making Reachy sad...")
            react_with_mood("sad")
            last_reaction_time = current_time

        # Motor controls: 1=left, 2=right, 3=stop
        elif key == ord("1"):
            send_motor_command("1")
        elif key == ord("2"):
            send_motor_command("2")
        elif key == ord("3"):
            send_motor_command("3")

        # Zone go-to: 4=zone0(0deg), 5=zone1(90deg), 6=zone2(180deg), 7=zone3(270deg)
        elif key == ord("4"):
            send_goto_position(0)
        elif key == ord("5"):
            send_goto_position(1)
        elif key == ord("6"):
            send_goto_position(2)
        elif key == ord("7"):
            send_goto_position(3)

        # Encoder commands
        elif key == ord("p"):
            pos = get_encoder_position()
            degrees = (pos / TICKS_PER_REV) * 360.0
            print(f"Encoder position: {pos} ticks ({degrees:.1f} degrees)")
        elif key == ord("z"):
            send_motor_command("z")
            print("Encoder zeroed (home set)")

        if key == ord("q"):
            break

except KeyboardInterrupt:
    print("Stopping...")
finally:
    if ser and ser.is_open:
        send_motor_command("3")  # stop motor on exit
        _serial_stop.set()
        ser.close()
    cap.release()
    cv2.destroyAllWindows()
