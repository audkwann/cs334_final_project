#!/usr/bin/env python3
"""
Ouija Board - A mystical spirit communication interface using a rotating table.

The table spells out responses letter-by-letter as an ancient spirit answers questions.

Physical layout (30 segments, 12 degrees each):
  Segment 0:     (blank/unused)
  Segments 1-26: A-Z
  Segment 27:    NO
  Segment 28:    GOODBYE
  Segment 29:    YES
"""

import os
import sys
import time
import random
import threading

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    print("Missing dependency: pyserial. Install with: pip install pyserial")
    sys.exit(1)

try:
    import speech_recognition as sr
    _speech_available = True
except ModuleNotFoundError:
    _speech_available = False
    print("speech_recognition not installed - voice input disabled.")
    print("Install with: pip install SpeechRecognition sounddevice")

try:
    import sounddevice as sd
    _sounddevice_available = True
except ModuleNotFoundError:
    _sounddevice_available = False

try:
    import requests
    _requests_available = True
except ModuleNotFoundError:
    _requests_available = False


# --- Constants ---

TICKS_PER_REV = 16567
NUM_SEGMENTS = 30
TICKS_PER_SEGMENT = TICKS_PER_REV // NUM_SEGMENTS  # ~552 ticks
LETTER_PAUSE = 2.0  # seconds to pause at each position

# Segment assignments
BLANK_SEGMENT = 0
NO_SEGMENT = 27
GOODBYE_SEGMENT = 28
YES_SEGMENT = 29

REACHY_API = "http://localhost:8000"

thinking_emotions = [
    "thoughtful1", "thoughtful2", "curious1", "inquiring1", "inquiring2",
    "uncertain1", "confused1", "attentive1", "attentive2"
]


# --- Segment Mapping ---

def char_to_segment(char: str) -> int:
    """Map a single letter or special word to its board segment.

    Segment layout:
      0     -> blank/unused
      1-26  -> A-Z
      27    -> NO
      28    -> GOODBYE
      29    -> YES

    Returns:
        Segment index (0-29), or -1 if the input is invalid.
    """
    char = char.upper().strip()

    if char == "YES":
        return YES_SEGMENT
    if char == "GOODBYE":
        return GOODBYE_SEGMENT
    if char == "NO":
        return NO_SEGMENT
    if len(char) == 1 and 'A' <= char <= 'Z':
        return ord(char) - ord('A') + 1
    return -1


def segment_to_label(segment: int) -> str:
    """Return the display label for a segment."""
    if segment == BLANK_SEGMENT:
        return "(blank)"
    if 1 <= segment <= 26:
        return chr(ord('A') + segment - 1)
    if segment == NO_SEGMENT:
        return "NO"
    if segment == GOODBYE_SEGMENT:
        return "GOODBYE"
    if segment == YES_SEGMENT:
        return "YES"
    return "?"


# --- Reachy API ---

def check_api_health():
    if not _requests_available:
        return False
    try:
        requests.get(f"{REACHY_API}/", timeout=3)
        print("Reachy API: connected")
        return True
    except Exception:
        print("Reachy API: not reachable - emotions disabled")
        return False


reachy_available = check_api_health()


def play_emotion(emotion_name: str):
    if not reachy_available or not _requests_available:
        return
    dataset_name = "pollen-robotics%2Freachy-mini-emotions-library"
    url = f"{REACHY_API}/api/move/play/recorded-move-dataset/{dataset_name}/{emotion_name}"
    try:
        response = requests.post(url, timeout=10)
        response.raise_for_status()
    except Exception:
        pass


# --- Serial / Arduino Setup ---

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
        print(f"Opened {arduino_port} - waiting for Arduino reset...")
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
                            print("  [motor] Zeroed")

        threading.Thread(target=_serial_reader, daemon=True).start()
    except serial.SerialException as exc:
        print(f"Failed to open {arduino_port}: {exc}")
        ser = None
else:
    print("Motor control disabled (no Arduino found).")


def send_serial(cmd: str):
    if ser and ser.is_open:
        ser.write(f"{cmd}\n".encode("ascii"))
        ser.flush()


def get_position() -> int:
    with _encoder_lock:
        return _encoder_position


def goto_ticks(ticks: int):
    send_serial(f"g{ticks}")


def shortest_target_ticks_for_segment(segment: int, current_ticks: int) -> int:
    """Return the equivalent absolute tick target for a segment using the shortest wraparound path.

    A circular position can be represented by many equivalent absolute tick values
    separated by +/- TICKS_PER_REV. We choose the one closest to the current encoder
    position so the table takes the shortest path.
    """
    base_ticks = (segment % NUM_SEGMENTS) * TICKS_PER_SEGMENT

    candidates = [
        base_ticks - TICKS_PER_REV,
        base_ticks,
        base_ticks + TICKS_PER_REV,
    ]

    return min(candidates, key=lambda t: abs(t - current_ticks))


def goto_segment(segment: int) -> int:
    """Move to a specific segment using the shortest circular path.

    Returns:
        The absolute target tick position that was commanded.
    """
    segment = segment % NUM_SEGMENTS
    current_ticks = get_position()
    target_ticks = shortest_target_ticks_for_segment(segment, current_ticks)
    label = segment_to_label(segment)

    print(
        f"  -> Moving to segment {segment} ({label}), "
        f"current={current_ticks}, target={target_ticks}"
    )

    goto_ticks(target_ticks)
    return target_ticks


def wait_for_position(target_ticks: int, timeout: float = 10.0) -> bool:
    """Block until the motor reaches the target position within tolerance."""
    tolerance = TICKS_PER_SEGMENT // 4
    start = time.time()
    while time.time() - start < timeout:
        pos = get_position()
        if abs(pos - target_ticks) <= tolerance:
            return True
        time.sleep(0.1)
    return False


# --- Speech Recognition ---

def listen_for_question() -> str | None:
    """Listen via microphone and return transcribed text, or None on failure."""
    if not _speech_available:
        print("[speech] speech_recognition not available, falling back to text input")
        return input("Ask the spirits a question: ").strip() or None

    if not _sounddevice_available:
        print("[speech] sounddevice not installed - falling back to text input")
        return input("Ask the spirits a question: ").strip() or None

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300
    recognizer.dynamic_energy_threshold = True

    try:
        sample_rate = 16000
        channels = 1
        duration_s = 15

        print("\n[listening] Speak your question to the spirits...")
        print(f"[listening] Recording up to {duration_s} seconds...")
        recording = sd.rec(
            int(duration_s * sample_rate),
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocking=True,
        )

        audio_bytes = recording.tobytes()
        audio = sr.AudioData(audio_bytes, sample_rate, sample_width=2)

        print("[processing] The spirits are listening...")
        text = recognizer.recognize_google(audio)
        print(f'[heard] "{text}"')
        return text.strip() if text.strip() else None

    except KeyboardInterrupt:
        raise
    except sr.UnknownValueError:
        print("[speech] Could not understand audio")
        return None
    except sr.RequestError as e:
        print(f"[speech] Recognition service error: {e}")
        return None
    except Exception as e:
        print(f"[speech] Microphone error: {e}")
        return None


# --- GPT Spirit Persona ---

SPIRIT_SYSTEM_PROMPT = """You are an ancient spirit communicating through a Ouija board.
You are mysterious and cryptic but not hostile.

RULES:
1. Respond with ONLY:
   - A single word (1-12 letters, A-Z only) to spell out
   - "YES" or "NO" for direct yes/no questions
   - "GOODBYE" to end the session (only if they say goodbye or want to stop)
2. Keep responses SHORT - the board must spell each letter slowly
3. Be cryptic but coherent
4. Favor mystical words like: SOON, BEWARE, FATE, NEVER, SEEK, WAIT, DREAM, TRUTH, SHADOW, BEYOND, SPIRIT, ETERNAL

IMPORTANT: Output ONLY the single word, nothing else. No punctuation, no explanation."""


def ask_spirit(question: str) -> str:
    """Ask the spirit (GPT-4o-mini) for a mystical response."""
    try:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print("[spirit] No OPENAI_API_KEY set, spirit speaks randomly")
            return random.choice(["YES", "NO", "SOON", "BEWARE", "FATE", "SEEK", "WAIT"])

        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=20,
            temperature=0.9,
            messages=[
                {"role": "system", "content": SPIRIT_SYSTEM_PROMPT},
                {"role": "user", "content": question}
            ]
        )

        answer = response.choices[0].message.content.strip().upper()
        answer = ''.join(c for c in answer if 'A' <= c <= 'Z')
        if not answer:
            answer = "SILENCE"
        print(f"[spirit] The spirit responds: {answer}")
        return answer

    except Exception as e:
        print(f"[spirit] Error consulting the spirit: {e}")
        return random.choice(["SOON", "BEWARE", "FATE", "SEEK"])


# --- Spelling ---

def spell_word(word: str, pause: float = LETTER_PAUSE):
    """Spell a response by moving to either a special segment or letter segments."""
    word = word.upper().strip()
    cleaned = ''.join(c for c in word if 'A' <= c <= 'Z')

    if not cleaned:
        return

    if cleaned in {"YES", "NO", "GOODBYE"}:
        print(f"\n  Spelling: {cleaned}")
        seg = char_to_segment(cleaned)
        target = goto_segment(seg)
        wait_for_position(target)
        time.sleep(pause)
        return

    print(f"\n  Spelling: {cleaned}")
    for i, char in enumerate(cleaned):
        seg = char_to_segment(char)
        if seg >= 0:
            print(f"  [{i + 1}/{len(cleaned)}] {char}", end="", flush=True)
            target = goto_segment(seg)
            wait_for_position(target)
            print(" ... done")
            time.sleep(pause)


# --- Main Session ---

def ouija_session():
    """Main Ouija board session loop."""
    print()
    print("=" * 50)
    print("       OUIJA BOARD - SPIRIT COMMUNICATION")
    print("=" * 50)
    print()
    print("The board awaits your questions...")
    print("Say 'goodbye' or press Ctrl+C to end the session.")
    print()

    session_active = True

    while session_active:
        try:
            question = listen_for_question()
            if not question:
                print("The spirits did not hear you. Try again.")
                continue

            if "goodbye" in question.lower() or "bye" in question.lower():
                print("\n[session] Ending session...")
                spell_word("GOODBYE")
                print("\nThe spirits depart. Farewell.")
                break

            print("\n[thinking] The spirits are contemplating...")
            play_emotion(random.choice(thinking_emotions))
            time.sleep(1.5)

            response = ask_spirit(question)
            spell_word(response)

            print("\n  The spirits await your next question...\n")
            time.sleep(1.0)

        except KeyboardInterrupt:
            print("\n\n[session] Interrupted (Ctrl-C). Saying goodbye, then returning to segment 0...")
            spell_word("GOODBYE")
            target = goto_segment(BLANK_SEGMENT)
            if ser and ser.is_open:
                wait_for_position(target, timeout=15.0)
            session_active = False


def print_segment_map():
    """Print the full board segment layout."""
    print()
    print("Ouija Board Segment Map (30 segments):")
    print("-" * 48)
    for seg in range(NUM_SEGMENTS):
        label = segment_to_label(seg)
        ticks = seg * TICKS_PER_SEGMENT
        degrees = seg * (360.0 / NUM_SEGMENTS)
        print(f"  Segment {seg:2d}: {label:8s} ({degrees:5.1f} deg, {ticks:5d} ticks)")
    print()


# --- Entry Point ---

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ouija Board Spirit Communication")
    parser.add_argument("--map", action="store_true", help="Print segment map and exit")
    parser.add_argument("--test", type=str, metavar="WORD", help="Test spelling a word")
    parser.add_argument("--segment", type=int, metavar="N", help="Go to segment N (0-29)")
    args = parser.parse_args()

    if args.map:
        print_segment_map()
        sys.exit(0)

    if args.segment is not None:
        if 0 <= args.segment < NUM_SEGMENTS:
            print(f"Moving to segment {args.segment} ({segment_to_label(args.segment)})")
            goto_segment(args.segment)
            time.sleep(5)
        else:
            print(f"Segment must be 0-{NUM_SEGMENTS - 1}")
        sys.exit(0)

    if args.test:
        print(f"Testing spelling: {args.test}")
        spell_word(args.test)
        sys.exit(0)

    try:
        ouija_session()
    except KeyboardInterrupt:
        print("\n\nInterrupted (Ctrl-C). Saying goodbye, then returning to segment 0...")
        spell_word("GOODBYE")
        target = goto_segment(BLANK_SEGMENT)
        if ser and ser.is_open:
            wait_for_position(target, timeout=15.0)
    finally:
        if ser and ser.is_open:
            send_serial("3")  # stop motor
            _serial_stop.set()
            ser.close()
        print("Done.")