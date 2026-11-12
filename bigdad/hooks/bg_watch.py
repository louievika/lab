"""Background job output watch (2.0.5, a PostToolUse part of post_tool.py, every session).

A background shell job writes to Claude Code's tasks/<id>.output, and the model polls that file. Reading it whole
again after it grew re-sends everything it already saw (live: up to 11 polls of one file). On a whole re-read of
such a file once it is 4,000+ bytes and at least twice the size it was at the last note: one short note, read just
the new end. A Read with offset/limit/pages, or a tail/head/sed of it, is never noted.

A background job piped into `| tail` (not tail -f) shows nothing until it ends, so a look at its output can't tell
alive from stuck (live: 32 such launches in 5 days). One short note at launch, once per chat and agent.
Off: BIGDAD_BG_WATCH=off.
"""
import json
import os
import re

import bd_common as C

MIN_BYTES = 4_000
MAX_FILES = 50                     # per chat: the state stays small
TASK_OUT = re.compile(r"(?i)[\\/]tasks[\\/]([A-Za-z0-9_-]{1,64})\.output$")
PIPE_TAIL = re.compile(r"\|\s*tail\b([^|;&\n]*)")
FOLLOW = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*[fF]|--follow)")


def state_path(event):
    sid = re.sub(r"[^A-Za-z0-9-]", "", C.dstr(event.get("session_id")) or "x")[:64] or "x"
    agent = re.sub(r"[^A-Za-z0-9-]", "", C.dstr(event.get("agent_id")))[:40] or "main"
    return os.path.join(C.state_dir(), f"bigdad-bgwatch-{sid}-{agent}.json")


def whole_reads(event):
    """Paths this call read whole: a Read with no offset/limit/pages, or a plain cat/type/Get-Content of the file."""
    tool = event.get("tool_name")
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "Read":
        fp = C.dstr(args.get("file_path"))
        if not fp or any(args.get(k) is not None for k in ("offset", "limit", "pages")):
            return []
        return [fp]
    if tool in ("Bash", "PowerShell"):
        import read_guard as R
        dump = R.parse_dump(args.get("command"), C.dstr(event.get("cwd")), shell=tool)
        return [p for p, r in (dump or []) if r == "FULL"]
    return []


def size_note(event):
    paths = [p for p in whole_reads(event) if TASK_OUT.search(p)]
    if not paths:
        return None
    st = state_path(event)
    notes = []
    with C.lock(st, wait=0.2):
        d = C.load_json(st)
        files = d.get("files") if isinstance(d.get("files"), dict) else {}
        for p in paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            key = os.path.normcase(os.path.abspath(p))
            prev = files.get(key) if isinstance(files.get(key), dict) else None
            seen = prev.get("seen", 0) if prev else 0
            noted = prev.get("noted", 0) if prev else 0
            seen = seen if isinstance(seen, int) else 0
            noted = noted if isinstance(noted, int) else 0
            if prev and size >= MIN_BYTES and size > seen and size >= 2 * noted:
                name = os.path.basename(p)
                notes.append(f"🥛 Big Dad: {name} (a background job's output) grew to ~{size // 4:,} tokens and each "
                             f"whole read re-sends all of it; read just the new end: tail -c +{seen + 1} on it, or "
                             "Read with offset.")
                noted = size
            files.pop(key, None)
            files[key] = {"seen": size, "noted": noted}
        while len(files) > MAX_FILES:
            files.pop(next(iter(files)))
        try:
            with open(st, "w", encoding="utf-8") as f:
                json.dump(dict(d, files=files), f)
        except OSError:
            pass
    return " ".join(notes) or None


def piped_into_tail(cmd):
    """True when a shell command pipes into tail without following (tail -f streams, so it is fine)."""
    if not isinstance(cmd, str):
        return False
    return any(not FOLLOW.search(m.group(1)) for m in PIPE_TAIL.finditer(cmd))


def tail_note(event):
    if event.get("tool_name") != "Bash":
        return None                # PowerShell's Select-Object -Last is not counted
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    resp = event.get("tool_response") if isinstance(event.get("tool_response"), dict) else {}
    if not (args.get("run_in_background") is True or C.dstr(resp.get("backgroundTaskId"))) or resp.get("interrupted"):
        return None
    if not piped_into_tail(args.get("command")):
        return None
    st = state_path(event)
    with C.lock(st, wait=0.2):
        d = C.load_json(st)
        if d.get("tail_noted"):
            return None
        d["tail_noted"] = True
        try:
            with open(st, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except OSError:
            return None
    return ("🥛 Big Dad: `| tail` holds back this background job's output until it ends, so a look at it can't tell "
            "alive from stuck; to watch it, write the output to a file (> run.log 2>&1) and tail that file.")


def main(event=None, standalone=True):
    if C.off("BIGDAD_BG_WATCH"):
        return None
    event = C.read_event() if event is None else event
    if not isinstance(event, dict) or event.get("hook_event_name") != "PostToolUse":
        return None
    text = []
    for part in (size_note, tail_note):
        try:
            t = part(event)
        except Exception:
            t = None
        if t:
            text.append(t)
    text = " ".join(text)
    if not text:
        return None
    if C.learning():
        C.learn_seen("bg_reread")                 # learning first (2.0.6): counted, no note
        return None
    return C.emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}, standalone)


if __name__ == "__main__":
    main()
