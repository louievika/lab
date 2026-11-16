#!/usr/bin/env python3
"""Big Dad status line: always on screen, zero model tokens (Claude Code shows it; the model never sees it).

  🥛█ Big Dad 🟢 💲71k/call · 💸 4.1M sent · 🧾 🥛 344k→63k · 📊 5h 12%
  🥛▄ Big Dad 🟡 💲160k/call · 1.2x guide · 💸 18.0M sent · /compact at a break
  🫗 Big Dad 🔴🔥 💲280k/call · 2.2x guide · 💸 31.5M sent · STOP: /compact now      (red, blinking, two frames)
  🚶 Dad's leaving 🔴🔥 💲340k/call · 2.6x guide · ...                                  (past red + 0.5x)
  🥛 Big Dad OFF: run /reload-plugins · 💲71k/call      (3+ tool calls and the per-tool-call hook never ran)

The glass empties as the chat grows toward red: the pause point if one is set, else 2x the guide. 💸 = every
input token this chat has sent so far; 🧾 = the last compact's measured call before→after (bd_money, read
incrementally from the log; 🥛 when it was Big Dad's slim compact; type `receipt` for every compact). 📊 = the plan's 5-hour limit, as Claude Code reports it (subscribers only).
Writes ~/.claude/bigdad/live.json, which the receipt reads. While Big Dad is idle (state folder refused), and on the
"Big Dad OFF" and no-tokens lines, the status line does not touch it; the receipt shows a reading only while the
file is younger than bd_money.LIVE_MAX_AGE_S (30 minutes), and any other reader must judge its age the same way.
Colors only when NO_COLOR is unset.
Turn on: `statusLine` in settings.json (with the user's yes).
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C
import bd_money as M

GREEN, YELLOW, RED, DIM, BOLD, END = "\033[32m", "\033[33m", "\033[1;31m", "\033[2m", "\033[1m", "\033[0m"
BLINK = "\033[5m"
LEVELS = "█▇▆▅▄▃▂▁"


def last_receipt(sid):
    """The guard's newest compact receipt for this chat (receipts.jsonl), for chats whose log no longer shows the drop."""
    best = None
    for line in C.tail_text(os.path.join(C.bd_dir(), "receipts.jsonl"), 32_000).splitlines():
        try:
            d = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(d, dict) and sid and d.get("session") == sid[:8] and isinstance(d.get("saved_per_call"), int):
            best = d
    return best


def tally(path, sid):
    """This chat's running receipt (bd_money.Tally): reads only the bytes added since the last refresh."""
    st_path = os.path.join(C.state_dir(), f"bigdad-burn-{sid or 'x'}.json")
    try:
        with open(st_path, encoding="utf-8", errors="replace") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError
    except (OSError, ValueError, RecursionError):
        st = {}
    try:
        size = os.path.getsize(path)
    except OSError:
        return M.Tally(guide=C.guard_at())
    ok = (st.get("v") == M.VERSION and st.get("path") == path and isinstance(st.get("off"), int)
          and not isinstance(st.get("off"), bool) and 0 <= st["off"] <= size)
    off = st["off"] if ok else 0
    t = M.Tally(st.get("t") if ok else None, C.guard_at())
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except OSError:
        return t
    end = data.rfind(b"\n") + 1               # only whole lines; the rest is read next time
    for raw in data[:end].splitlines():
        t.feed(raw)
    try:
        with open(st_path, "w", encoding="utf-8") as f:
            json.dump({"v": M.VERSION, "path": path, "off": off + end, "t": t.to_dict()}, f)
    except OSError:
        pass
    return t


def plan_of(event):
    """Claude Code's plan-limit numbers from the status line input (`rate_limits`: subscribers, or a gateway limit).
    Tenth review: a limit is kept by bd_money's own rule (limit_shown, and bd_common's is_number), the one the receipts read with."""
    rl = event.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    out = {}
    for k in ("five_hour", "seven_day", "spend_limit"):
        v = rl.get(k)
        if M.limit_shown(v):
            at = v.get("resets_at")
            out[k] = {"used_percentage": v["used_percentage"], "resets_at": at if C.is_number(at) else None}
    return out or None


def line(event, color=True, now=None):
    path = C.dstr(event.get("transcript_path"))
    sid = C.dstr(event.get("session_id"))
    tokens = C.last_call(path)[0] if path else 0
    ws = event.get("workspace")
    C.EVENT_CWD = C.dstr(event.get("cwd")) or (C.dstr(ws.get("current_dir")) if isinstance(ws, dict) else "")
    limit = C.guard_at()
    pause = C.pause_at()
    stop = pause or 2.0                       # red: the pause point if set, else 2x the guide
    paint = (lambda c, s: f"{c}{s}{END}") if color else (lambda c, s: s)
    if C.state_problem():                       # 2.0.9: no private state folder, so the hooks stay idle (bd_common)
        # 3.0.0 fifth review: no file operation on live.json here; its reader judges its age (bd_money.plan)
        return f"🥛 {paint(YELLOW, 'Big Dad idle')}: state folder refused · `bigdad.py health`" + (
            f" · 💲{tokens // 1000:,}k/call" if tokens else "")
    if path and C.hooks_off(path, sid):        # the status line runs from settings.json; the hooks may not
        return f"🥛 {paint(RED, 'Big Dad OFF')}: run /reload-plugins" + (f" · 💲{tokens // 1000:,}k/call" if tokens else "")
    if not tokens:
        return f"🥛█ {paint(BOLD, 'Big Dad')} 🟢 {paint(DIM, f'watching · guide {limit // 1000}k/call')}"
    x = tokens / limit
    level = max(0.0, 1 - x / stop)             # full glass at 0, empty at the pause point
    glass = "🥛" + LEVELS[min(len(LEVELS) - 1, int((1 - level) * len(LEVELS)))] if level > 0 else "🫗"
    head = "🚶 " + paint(RED, "Dad's leaving") if x >= stop + 0.5 else f"{glass} {paint(BOLD, 'Big Dad')}"
    money = f"💲{tokens // 1000:,}k/call"
    t = tally(path, sid)
    fire = f" · 💸 {M.big(t.sent)} sent" if t.sent else ""
    if x < 1:
        state, out = "green", f"{head} 🟢 {paint(GREEN, money)}{paint(DIM, fire)}"
    elif x < stop:
        state, out = "yellow", f"{head} 🟡 {paint(YELLOW, money)} · {x:.1f}x guide{fire} · /compact at a break"
    else:
        frame = int(now if now is not None else time.time()) % 2       # each refresh flips the alarm
        alarm = "🔴🔥" if frame == 0 else "🔥🔴"
        state = "red"
        out = f"{head} {alarm} {paint(RED + BLINK, money + f' · {x:.1f}x guide{fire} · STOP: /compact now')}"
    last_cut = t.cuts[-1] if t.cuts else None
    r = None if last_cut else last_receipt(sid)
    drop = (last_cut[2], last_cut[3]) if last_cut else (r.get("before"), r.get("after")) if r else None
    # thirty-second review: in range too (C.sane), so a 400-digit receipt row cannot raise at the division below
    drop = drop if drop and all(C.sane_ok(v, whole=True) for v in drop) and drop[0] > drop[1] else None
    per_call = drop[0] - drop[1] if drop else None
    if drop:                                    # the last compact, measured: the call before -> the call after
        mark = "🥛 " if last_cut and last_cut[1].startswith("slim ") else ""
        out += " · 🧾 " + paint(GREEN, f"{mark}{M.big(drop[0])}→{M.big(drop[1])}")
    plan = plan_of(event)
    five = (plan or {}).get("five_hour")
    if five:
        p = five["used_percentage"]
        out += " · 📊 5h " + paint(RED if p >= 80 else YELLOW if p >= 50 else DIM, f"{p:.0f}%")
    try:                                        # for the receipt: the same numbers, machine-readable (not kept up while idle)
        tmp = os.path.join(C.bd_dir(), "live.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": C.now(), "session": sid[:8], "tokens": tokens, "guide": limit, "x": round(x, 2),
                       "pause_at": pause, "state": state, "milk": round(level, 2), "sent": t.sent,
                       "last_compact": list(drop) if drop else None, "saved_per_call": per_call, "plan": plan}, f)
        os.replace(tmp, os.path.join(C.bd_dir(), "live.json"))
    except OSError:
        pass
    return out


def main():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        event = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):        # eleventh review: too deeply nested is bad input too
        event = {}
    if not isinstance(event, dict):
        event = {}
    try:
        text = line(event, color=not os.environ.get("NO_COLOR"))
    except Exception:
        text = "🥛 Big Dad"                     # a status line must never show a traceback
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
    except (AttributeError, ValueError):
        print(text, end="")


if __name__ == "__main__":
    main()
