#!/usr/bin/env python3
"""Big Dad watch (Claude Code PostToolUse hook).

Run `bigdad.py watch` in a session and Big Dad keeps watching it for the rest of that
session: tool outputs over ~5k tokens get logged (tool name and size only, never the
content; a Bash output over ~30k chars is counted as the ~2k-char preview the model actually
keeps, since Claude Code saves the rest to a file), and the agent is asked, once, to note any
waste or Big Dad improvement it sees with `bigdad.py note "..."`. Notes are read back with
`bigdad.py notes`. Nothing changes on its own: a note becomes a change only with the user's yes.
Subagents of a watched session are watched too (1.7.0): their big outputs go into the same notes with the agent's
type and short id, and they get no message (a subagent can't start or stop the watch).

  BIGDAD_WATCH=off          turn it off
  BIGDAD_WATCH_BIG=20000    size (characters) that counts as a big output
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

NOTES = os.environ.get("BIGDAD_NOTES") or os.path.join(C.bd_dir(), "notes.jsonl")
START = re.compile(r"bigdad\.py[\"']?\s+watch\b")
BASH_INLINE = int(os.environ.get("BIGDAD_BASH_INLINE") or 30_000)   # over this, Bash output goes to a file...
BASH_PREVIEW = 2_000                                                  # ...and the model keeps about this much


def main(event=None, standalone=True):
    if C.off("BIGDAD_WATCH"):
        return
    event = C.read_event() if event is None else event
    if event is None:
        return
    agent = C.dstr(event.get("agent_id"))          # a subagent shares its parent's session_id, so its watch flag
    sid = C.dstr(event.get("session_id")) or "x"
    flag = C.watch_flag(sid)
    tool = C.dstr(event.get("tool_name")) or "?"
    cmd = C.dstr(C.dget(event.get("tool_input"), "command"))
    if not agent and tool == "Bash" and START.search(cmd) and not os.path.exists(flag):
        try:
            with open(flag, "w") as f:
                json.dump({"since": C.now()}, f)
        except OSError:
            return
        C.append_jsonl(NOTES, {"ts": C.now(), "session": sid[:8], "kind": "watch-start"})
        return C.emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
            "🥛 Big Dad is watching this session. When you see tokens wasted (big outputs, rereads, "
            "unused tools, a costly subagent) or a way Big Dad itself could do better, run "
            "`bigdad.py note \"<one line: what happened, what to change>\"`. No paths or secrets in notes. "
            "Don't change Big Dad from a note without the user's yes."}}, standalone)
    if not os.path.exists(flag):
        return
    try:
        big = int(os.environ.get("BIGDAD_WATCH_BIG") or 20_000)
    except ValueError:
        big = 20_000
    size, images = C.est(event.get("tool_response"))     # images count as images (~1.6k tokens), never as base64
    kept = size
    if tool == "Bash" and size > BASH_INLINE:        # Claude Code saves it to a file; the model keeps a short preview
        kept = BASH_PREVIEW
    if size >= big:
        row = {"ts": C.now(), "session": sid[:8], "kind": "big-output", "tool": tool, "chars": size, "tokens": kept // 4}
        if agent:
            row["agent"] = agent[:8]
            row["agent_type"] = C.dstr(event.get("agent_type"))[:40] or "?"
        if kept != size:
            row["kept_chars"] = kept
        if images:
            row["images"] = images
        C.append_jsonl(NOTES, row)


if __name__ == "__main__":
    main()
