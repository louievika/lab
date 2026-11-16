#!/usr/bin/env python3
"""Big Dad PostToolUse entry point: context guard + watch + output shrink (+ loop detector while watched) in ONE process (one Python start per event instead of two).
Each part runs in its own try/except, so one failing never blocks the other; outputs merge into one hook
response. context_guard.py and watch.py still run on their own.
2.0.6: while Big Dad learns first (bd_common.learning), at most every 2 minutes a refresh that ends learning once
the data is in; then the one line that it is on (bd_common.learned_note), once."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C


def main():
    event = C.read_event(need_state=False)
    if event is None:
        return
    if C.state_problem():              # 3.0.0 review: refused state folder; every part stays idle except the pause the
        try:                           # user set, which needs only the log and the settings (nothing is written)
            import context_guard
            C.emit(context_guard.refused_pause(event))
        except Exception:
            pass
        return
    if not C.dstr(event.get("agent_id")):          # heartbeat: the per-tool-call guard is running in this session
        try:
            open(C.beat_file(C.dstr(event.get("session_id")) or "x"), "w").close()
        except OSError:
            pass
    parts = ["shrink", "read_guard", "bg_watch", "context_guard", "watch"]  # shrink: big noisy Bash output cut
    #   (updatedToolOutput), first, so the parts after it see the output the model will get; read_guard: marks a
    #   Read's result as in context; bg_watch: a background job's growing output read whole again (2.0.5)
    if os.path.exists(C.watch_flag(C.dstr(event.get("session_id")) or "x")):
        parts.append("loop_guard")     # v1.6: the 400 KB loop scan runs only in a watched session (`bigdad.py watch`)
    if C.learning():                   # 2.0.6: learning first; a refresh now and then ends it when the data is in
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            "skills", "bigdad", "scripts"))
            __import__("bd_learn").tick(C.dstr(event.get("cwd")))
        except Exception:
            pass
    out = C.run_parts(parts, event)
    told = C.learned_note()            # learning just ended: one line, once
    if told:
        out = out if isinstance(out, dict) else {}
        out["systemMessage"] = (told + " " + out.get("systemMessage", "")).strip()
    C.emit(out)


if __name__ == "__main__":
    main()
