#!/usr/bin/env python3
"""Big Dad loop detector (runs inside post_tool.py on PostToolUse; also standalone).

The same tool with the same input 4+ times in the last 20 calls of a thread (a subagent or the
main chat), with no file edit in between, gets a one-line note to that thread: "you have run X 4 times; change approach or report
back". Noted once per 4 repeats, never blocks anything. Cheap: reads only the tail of the thread's
own transcript. Subagent loops are also flagged in agents.jsonl for `bigdad.py agents`.

  BIGDAD_LOOP=off          turn it off
  BIGDAD_LOOP_TIMES=4      repeats that count as a loop
"""
import hashlib, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

WINDOW = 20
EDITS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
NOISE = {"description", "timeout"}


def key_of(name, inp):
    if isinstance(inp, dict):           # Claude writes a fresh description per call ("run 2 of 4"): not part of the call
        inp = {k: v for k, v in inp.items() if k not in NOISE}
    return (C.dstr(name) or "?", json.dumps(inp, sort_keys=True, default=str))


def recent_calls(path, n=WINDOW, ids=None):
    """The last n (tool name, input) pairs in a transcript, newest first. Fills `ids` with their tool_use ids."""
    out, seen = [], set()
    for line in reversed(C.tail_text(path, 400_000).splitlines()):
        if '"tool_use"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") if isinstance(d, dict) else None
        if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for b in reversed(m["content"]):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                key = (C.dstr(m.get("id")), C.dstr(b.get("id")))
                if all(key) and key in seen:                 # the same message logged twice is not a repeat
                    continue
                seen.add(key)
                if C.dstr(b.get("name")) in EDITS:
                    return out              # something changed: repeats before this edit aren't a loop
                try:
                    out.append(key_of(b.get("name"), b.get("input")))
                    if ids is not None:
                        ids.add(C.dstr(b.get("id")))
                except (TypeError, ValueError):
                    continue
                if len(out) >= n:
                    return out
    return out


def main(event=None, standalone=True):
    if C.off("BIGDAD_LOOP"):
        return
    event = C.read_event() if event is None else event
    if event is None:
        return
    try:
        times = int(os.environ.get("BIGDAD_LOOP_TIMES") or 4)
    except ValueError:
        times = 4
    agent = C.dstr(event.get("agent_id"))
    sid = C.dstr(event.get("session_id")) or "x"
    path = C.dstr(event.get("transcript_path"))
    if agent:
        import context_guard
        path = context_guard.agent_transcript(event, path, sid, agent)
    if not path or not os.path.isfile(path):
        return
    ids = set()
    calls = recent_calls(path, ids=ids)
    # the call that just ran may not be in the log yet when the hook fires (seen live): count it from the event
    tid = C.dstr(event.get("tool_use_id"))
    if event.get("tool_name") and (not tid or tid not in ids):
        calls = [key_of(event.get("tool_name"), event.get("tool_input"))] + calls[:WINDOW - 1]
    if len(calls) < times:
        return
    counts = {}
    for key in calls:
        counts[key] = counts.get(key, 0) + 1
    worst = max(counts.items(), key=lambda kv: kv[1])
    (tool, inp), n = worst
    if n < times:
        return
    state = os.path.join(C.state_dir(), f"bigdad-loop-{sid}-{agent or 'main'}.json")
    h = hashlib.sha1((tool + inp).encode("utf-8", "replace")).hexdigest()[:12]
    try:
        noted = json.load(open(state))
        noted = noted if isinstance(noted, dict) else {}
    except (OSError, ValueError):
        noted = {}
    if n < (noted.get(h) or 0) + times:
        return                          # already said at this count; say it again after `times` more repeats
    noted[h] = n
    try:
        with open(state, "w") as f:
            json.dump(noted, f)
    except OSError:
        pass
    hook = C.dstr(event.get("hook_event_name")) or "PostToolUse"
    if agent:
        C.append_jsonl(os.path.join(C.bd_dir(), "agents.jsonl"), {
            "ts": C.now(), "event": "loop", "session": sid[:8], "agent_id": agent[:17],
            "agent_type": C.dstr(event.get("agent_type")), "tool": tool, "times": n, "window": len(calls)})
    return C.emit({"hookSpecificOutput": {"hookEventName": hook, "additionalContext": (
        f"🥛 Big Dad 🔁 loop: you have run {tool} with the same input {n} times in your last {len(calls)} calls; "
        "change approach or report back (say what you tried and what blocked it).")}}, standalone)


if __name__ == "__main__":
    main()
