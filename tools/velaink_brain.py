#!/usr/bin/env python3
"""VelaInk (墨灵) — Linux 侧 AI 大脑笔迹生成器

把 SVG 路径（LLM 生成或矢量文件）解析成归一化笔迹，
再通过 VELAINK/1 协议送往 openvela 实时层。

链路位置：
    LLM/视觉 ----SVG----> 本脚本 ----VELAINK/1----> openvela velaink --G-code--> GRBL

用法：
    # 解析 SVG 文件里所有 <path d="...">，输出到 stdout
    python3 velaink_brain.py drawing.svg

    # 直接给一段 path 数据
    python3 velaink_brain.py --path "M10 10 L90 10 L90 90 Z"

    # 发送到串口（需要 pyserial）
    python3 velaink_brain.py drawing.svg --dev /dev/ttyUSB0 --baud 115200

    # 内置示例（一颗心），用于自检
    python3 velaink_brain.py --demo

输出协议（纯文本，可肉眼调试）：
    VELAINK/1
    S
    P <x> <y>
    ...
    E
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from typing import Iterable, List, Tuple

Point = Tuple[float, float]
Stroke = List[Point]

# 内置示例：一颗心（与 openvela 端自检一致，便于对照）
DEMO_PATH = (
    "M50 30 C50 30 45 15 30 15 C12 15 8 32 8 45 "
    "C8 62 25 75 50 90 C75 75 92 62 92 45 "
    "C92 32 88 15 70 15 C55 15 50 30 50 30 Z"
)

TOKEN_RE = re.compile(r"([MLCQZmlcqz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")


def _tokenize(d: str) -> Iterable[Tuple[str, List[float]]]:
    """把 SVG path 的 d 属性切成 (命令, 参数) 序列。"""
    cmd = None
    args: List[float] = []
    for tok in TOKEN_RE.finditer(d):
        letter, number = tok.group(1), tok.group(2)
        if letter:
            if cmd:
                yield cmd, args
            cmd, args = letter, []
        else:
            args.append(float(number))
    if cmd:
        yield cmd, args


def _bezier3(p0: Point, p1: Point, p2: Point, p3: Point, n: int) -> List[Point]:
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = (mt**3) * p0[0] + 3 * (mt**2) * t * p1[0] + 3 * mt * (t**2) * p2[0] + (t**3) * p3[0]
        y = (mt**3) * p0[1] + 3 * (mt**2) * t * p1[1] + 3 * mt * (t**2) * p2[1] + (t**3) * p3[1]
        out.append((x, y))
    return out


def _bezier2(p0: Point, p1: Point, p2: Point, n: int) -> List[Point]:
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = (mt**2) * p0[0] + 2 * mt * t * p1[0] + (t**2) * p2[0]
        y = (mt**2) * p0[1] + 2 * mt * t * p1[1] + (t**2) * p2[1]
        out.append((x, y))
    return out


def parse_path(d: str, curve_steps: int = 24) -> List[Stroke]:
    """解析单条 SVG path，返回笔画列表（每个 M 或 Z 之后断开）。"""
    strokes: List[Stroke] = []
    cur: Stroke = []
    start: Point = (0.0, 0.0)
    pos: Point = (0.0, 0.0)

    def flush() -> None:
        nonlocal cur
        if len(cur) >= 2:
            strokes.append(cur)
        cur = []

    for cmd, args in _tokenize(d):
        rel = cmd.islower()
        c = cmd.upper()
        if not rel:
            pass
        if c == "M":
            flush()
            x, y = args[0], args[1]
            if rel:
                x, y = pos[0] + x, pos[1] + y
            pos = (x, y)
            start = pos
            cur = [pos]
            # 后续的隐式 lineto
            for i in range(2, len(args) - 1, 2):
                x, y = args[i], args[i + 1]
                if rel:
                    x, y = pos[0] + x, pos[1] + y
                pos = (x, y)
                cur.append(pos)
        elif c == "L":
            for i in range(0, len(args) - 1, 2):
                x, y = args[i], args[i + 1]
                if rel:
                    x, y = pos[0] + x, pos[1] + y
                pos = (x, y)
                cur.append(pos)
        elif c == "C":
            for i in range(0, len(args) - 5, 6):
                p1 = (args[i], args[i + 1])
                p2 = (args[i + 2], args[i + 3])
                p3 = (args[i + 4], args[i + 5])
                if rel:
                    p1 = (pos[0] + p1[0], pos[1] + p1[1])
                    p2 = (pos[0] + p2[0], pos[1] + p2[1])
                    p3 = (pos[0] + p3[0], pos[1] + p3[1])
                cur.extend(_bezier3(pos, p1, p2, p3, curve_steps))
                pos = p3
        elif c == "Q":
            for i in range(0, len(args) - 3, 4):
                p1 = (args[i], args[i + 1])
                p2 = (args[i + 2], args[i + 3])
                if rel:
                    p1 = (pos[0] + p1[0], pos[1] + p1[1])
                    p2 = (pos[0] + p2[0], pos[1] + p2[1])
                cur.extend(_bezier2(pos, p1, p2, curve_steps))
                pos = p2
        elif c == "Z":
            cur.append(start)
            flush()
    flush()
    return strokes


def normalize(strokes: List[Stroke], keep_aspect: bool = True) -> List[Stroke]:
    """归一化到 0.0~1.0，并把 Y 轴翻转（SVG 向下为正 -> 机器向上为正）。"""
    pts = [p for s in strokes for p in s]
    if not pts:
        return []
    minx = min(p[0] for p in pts)
    maxx = max(p[0] for p in pts)
    miny = min(p[1] for p in pts)
    maxy = max(p[1] for p in pts)
    w = maxx - minx or 1.0
    h = maxy - miny or 1.0

    out: List[Stroke] = []
    for s in strokes:
        ns: Stroke = []
        for x, y in s:
            nx = (x - minx) / w
            ny = (maxy - y) / h
            if keep_aspect:
                # 等比缩放并居中，避免图形被拉伸
                scale = min(w, h)
                nx = (x - minx) / scale
                ny = (maxy - y) / scale
                nx -= (w / scale - 1.0) / 2.0
                ny += 0.0
            ns.append((round(nx, 5), round(ny, 5)))
        out.append(ns)
    return out


def to_protocol(strokes: List[Stroke]) -> str:
    """序列化为 VELAINK/1 协议文本。"""
    lines = ["VELAINK/1"]
    for s in strokes:
        lines.append("S")
        for x, y in s:
            lines.append(f"P {x:.5f} {y:.5f}")
    lines.append("E")
    return "\n".join(lines) + "\n"


def paths_from_svg(path: str) -> List[str]:
    """从 SVG 文件里抽出所有 path 的 d 属性。"""
    tree = ET.parse(path)
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    ds = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "path":
            d = el.get("d")
            if d:
                ds.append(d)
    return ds


def main() -> int:
    ap = argparse.ArgumentParser(description="VelaInk AI brain: SVG -> strokes -> openvela")
    ap.add_argument("svg", nargs="?", help="SVG 文件（提取所有 <path>）")
    ap.add_argument("--path", help="直接给出 SVG path 的 d 数据")
    ap.add_argument("--demo", action="store_true", help="使用内置示例（一颗心）")
    ap.add_argument("--dev", help="串口设备，如 /dev/ttyUSB0（需要 pyserial）")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("-o", "--out", help="输出到文件（默认 stdout）")
    args = ap.parse_args()

    if args.demo:
        ds = [DEMO_PATH]
    elif args.path:
        ds = [args.path]
    elif args.svg:
        ds = paths_from_svg(args.svg)
    else:
        ap.error("需要 svg 文件、--path 或 --demo 之一")

    strokes: List[Stroke] = []
    for d in ds:
        strokes.extend(parse_path(d))
    if not strokes:
        print("velaink_brain: 没有解析出任何笔画", file=sys.stderr)
        return 1

    strokes = normalize(strokes)
    text = to_protocol(strokes)

    if args.dev:
        try:
            import serial  # type: ignore
        except ImportError:
            print("velaink_brain: 需要 pyserial（pip install pyserial）", file=sys.stderr)
            return 2
        with serial.Serial(args.dev, args.baud, timeout=1) as ser:
            ser.write(text.encode())
        print(f"velaink_brain: 已发送 {len(strokes)} 笔 / "
              f"{sum(len(s) for s in strokes)} 点到 {args.dev}", file=sys.stderr)
    elif args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"velaink_brain: 已写入 {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
