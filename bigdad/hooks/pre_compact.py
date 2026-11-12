#!/usr/bin/env python3
"""Big Dad PreCompact entry point: read guard reset + slim compact instructions in ONE process (one Python start per event instead of two).
Each part runs in its own try/except, so one failing never blocks the other; outputs merge into one hook
response. The user's own /compact text still comes first (compact_slim puts it there); read_guard prints nothing,
and neither does context_guard (2.0.5: it only notes the size the compact starts from, for the receipt)."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C


CAP = 9_500     # Claude Code 2.1.314 cuts PreCompact stdout at 10,000 chars; stay under it


def clamp(text, cap=CAP):
    """Cut text so its length in UTF-16 units (how the cap is counted) stays under cap; never splits a pair."""
    if not isinstance(text, str):
        return None
    if len(text.encode("utf-16-le")) // 2 <= cap:
        return text
    out, n = [], 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > cap:
            break
        out.append(ch)
        n += w
    return "".join(out)


def main():
    try:                       # never break a compact: no instructions is the safe failure
        event = C.read_event()
        if event is None:
            return
        C.emit(clamp(C.run_parts(("read_guard", "compact_slim", "context_guard"), event)))
    except Exception:
        return


if __name__ == "__main__":
    main()
