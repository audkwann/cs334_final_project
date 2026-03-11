# CS334 Final Project - Interactive Rotating Table

A rotating table system with encoder-based positioning, controlled by Arduino and interfaced through Python. Features multiple interaction modes including object recognition with Reachy robot emotions, and a Ouija board spirit communication interface.

## Hardware

- **Arduino UNO** with BTS7960 motor driver
- **Rotary encoder** (1425.1 PPR) on pins 2/3
- **Belt-driven table** with 11.625:1 ratio (16,567 ticks per revolution)
- **Reachy Mini robot** (optional) for emotional expressions
- **Microphone** for voice input

### Wiring

| Component | Arduino Pin |
|-----------|-------------|
| RPWM | 5 |
| LPWM | 6 |
| REN | 7 |
| LEN | 8 |
| Encoder A | 2 (interrupt) |
| Encoder B | 3 (interrupt) |

## Installation

```bash
pip install pyserial SpeechRecognition pyaudio openai requests
pip install opencv-python pillow transformers  # for cs334_v5.py
```

Set your OpenAI API key:
```bash
export OPENAI_API_KEY=sk-your-key-here
```

Upload `pulley.ino` to the Arduino.

## Scripts

### ouija.py - Ouija Board Spirit Communication

A mystical Ouija board that spells out responses letter-by-letter. Uses GPT-4o-mini as an "ancient spirit" that gives cryptic one-word answers.

**Layout (29 segments):**
- Segment 0: YES
- Segment 1: GOODBYE
- Segment 2: NO
- Segments 3-28: A through Z

```bash
# Run full Ouija session (voice or text input)
python ouija.py

# Print segment map
python ouija.py --map

# Test spelling a word
python ouija.py --test HELLO

# Test moving to a specific segment
python ouija.py --segment 14
```

### table_control.py - Manual Table Control

Interactive command-line control for the rotating table. 30 segments at 12 degrees each.

```bash
python table_control.py
```

Commands:
- `<number>` - Go to segment 0-29
- `d <degrees>` - Go to exact degrees
- `z` - Zero encoder at current position
- `p` - Print current position
- `s` - Stop motor
- `q` - Quit

### cs334_v5.py - Reachy Object Recognition

Full interactive system with camera, BLIP captioning, and Reachy robot emotions. Press keys in the OpenCV window:

- `SPACE` - Capture frame and react based on emotion rules
- `a` - Ask Reachy a yes/no question (voice input)
- `y/n` - Manually trigger happy/sad emotion
- `1/2/3` - Motor left/right/stop
- `4-7` - Go to zones 0-3 (90-degree increments)
- `p` - Print encoder position
- `z` - Zero encoder
- `q` - Quit

### fetch_item.py - Item Retrieval

Rotate to a specified degree, let Reachy observe, then return to home position.

## Arduino Commands (pulley.ino)

Serial commands at 115200 baud:

| Command | Action |
|---------|--------|
| `1` | Spin left |
| `2` | Spin right |
| `3` | Soft stop |
| `p` | Report encoder position |
| `z` | Zero encoder (set home) |
| `g<ticks>` | Go to absolute position (e.g., `g1425`) |

## Architecture

```
[Microphone] -> [Speech Recognition] -> [Python Script]
                                              |
                                              v
                                        [OpenAI GPT]
                                              |
                                              v
[Arduino] <-- Serial (115200) <-- [Motor Commands]
    |
    v
[BTS7960 Driver] -> [Motor] -> [Belt] -> [Table]
    ^
    |
[Encoder] -- position feedback
```

## Encoder Math

- Encoder PPR: 1425.1
- Belt ratio: 11.625:1
- **Ticks per revolution: 16,567**
- Position accuracy: ~0.022 degrees
