#!/usr/bin/env python3
"""Simple motor control: 1=left, 2=right, 3=stop, q=quit."""

import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    print("Missing dependency: pyserial. Install with: pip install pyserial")
    sys.exit(1)


def find_arduino_port():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    for p in ports:
        if p.device.startswith(("/dev/tty.usb", "/dev/cu.usb")) or "arduino" in (p.description or "").lower():
            return p.device
    print("No Arduino found. Available ports:")
    for p in ports:
        print(f"  {p.device} - {p.description or 'Unknown'}")
    return None


port = find_arduino_port()
if not port:
    sys.exit(1)

ser = serial.Serial(port, 115200, timeout=0.25)
print(f"Opened {port} — waiting for Arduino reset...")
time.sleep(2.0)

print("\nMotor Control:")
print("  1 = left")
print("  2 = right")
print("  3 = stop")
print("  q = quit\n")

try:
    while True:
        cmd = input(">> ").strip()
        if cmd == "q":
            break
        if cmd in ("1", "2", "3"):
            ser.write(f"{cmd}\n".encode("ascii"))
            ser.flush()
            print(f"  Sent: {cmd}")
        else:
            print("  Unknown command. Use 1, 2, 3, or q.")
except KeyboardInterrupt:
    print("\nStopping...")
finally:
    ser.write(b"3\n")
    ser.flush()
    ser.close()
    print("Done.")
