#!/usr/bin/env python3
"""VelaInk 演示：把 G-code 文件按 GRBL 流控发给写字机（CH340G, 115200）

用法：
    python gcode_send.py COM3 ../Desktop/VelaInk/velaink_demo_heart.nc
    python gcode_send.py COM3 --list   # 列出可用串口
"""
import sys
import time

def list_ports():
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            print(f"{p.device}  {p.description}")
    except ImportError:
        print("需要 pyserial: pip install pyserial")

def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        list_ports(); return 0
    if len(sys.argv) < 3:
        print(__doc__); return 1
    port, path = sys.argv[1], sys.argv[2]

    import serial
    ser = serial.Serial(port, 115200, timeout=1)
    time.sleep(2)          # GRBL 重启
    ser.reset_input_buffer()
    ser.write(b"\r\n\r\n")
    time.sleep(2)
    ser.reset_input_buffer()
    # 打印启动信息
    t0 = time.time()
    while time.time() - t0 < 1:
        line = ser.readline().decode(errors="ignore").strip()
        if line:
            print("<<", line)

    # 注：本机 $22=0（归零未启用），不发 $H；起始位置以当前人为对好的原点为准

    print(f">> streaming {path}")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith(("(", ";"))]

    for i, cmd in enumerate(lines):
        ser.write((cmd + "\n").encode())
        while True:
            resp = ser.readline().decode(errors="ignore").strip()
            if resp == "ok":
                break
            if resp.startswith("error"):
                print(f"!! GRBL error at line {i}: {cmd} -> {resp}")
                ser.close(); return 1
            if resp:
                print("<<", resp)
        if i % 20 == 0:
            print(f"   ... {i}/{len(lines)}")

    print(">> done, returning to origin")
    ser.write(b"G0 X0 Y0\n")
    time.sleep(0.1)
    ser.close()
    print("DONE")
    return 0

if __name__ == "__main__":
    sys.exit(main())
