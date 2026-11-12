#!/usr/bin/env python3
"""Big Dad agent monitor (Claude Code SubagentStart + SubagentStop hooks).

Logs every subagent to ~/.claude/bigdad/agents.jsonl: start (id, type), stop (id, type,
model, calls, last call size, total input tokens, read from the agent's own transcript).
Numbers only: no prompts, no paths, no output. `bigdad.py agents` reads it back with the
running agents' size now, and flags: several agents on the priciest model tier at once,
an agent past the per-call guide, a fork carrying a big chat, an agent with no explicit model.

  BIGDAD_AGENTS=off   turn it off
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C


def totals(path):
    """calls, total input tokens, last call input, model, max output tokens: one pass over the agent transcript."""
    calls = total = last = 0
    model = ""
    seen = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") if isinstance(d, dict) else None
                if not isinstance(m, dict):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict) or m.get("id") in seen:
                    continue
                seen.add(m.get("id"))
                n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                        if isinstance(u.get(k), int))
                if not n:
                    continue
                calls += 1
                total += n
                last = n
                model = C.dstr(m.get("model")) or model
    except (OSError, TypeError, ValueError):
        pass
    return calls, total, last, model


def main():
    if C.off("BIGDAD_AGENTS"):
        return
    event = C.read_event()
    if event is None:
        return
    agent = C.dstr(event.get("agent_id"))
    if not agent:
        return
    hook = C.dstr(event.get("hook_event_name"))
    row = {"ts": C.now(), "session": (C.dstr(event.get("session_id")) or "x")[:8],
           "agent_id": agent[:17], "agent_type": C.dstr(event.get("agent_type")) or "?"}
    if hook == "SubagentStop":
        if row["agent_type"] == "?":
            return                     # headless `claude -p --resume` fires a SubagentStop with no subagent behind it
        row["event"] = "stop"
        calls, total, last, model = totals(C.dstr(event.get("agent_transcript_path")))
        row.update({"calls": calls, "input_total": total, "last_call": last, "model": model})
        relaunch(event, agent, row)
    else:
        row["event"] = "start"
    log = os.path.join(C.bd_dir(), "agents.jsonl")
    with C.lock(log):                  # two copies of the plugin fire the same event: log it once
        if duplicate(log, row):
            return
        if row["event"] == "stop" and not started(log, row):
            return                     # a stop with no start is a phantom (no subagent ran): no row
        C.append_jsonl(log, row)


def relaunch(event, agent, row):
    """An agent the hard stop capped has stopped: queue ONE line for the main chat's next hook (the main's guard
    reads it), with what it cost and the handoff to relaunch from. The stop's state file goes, so it's said once."""
    sid = C.dstr(event.get("session_id")) or "x"
    state = os.path.join(C.state_dir(), f"bigdad-stop-{sid}-{agent}.json")
    if not os.path.exists(state):
        return
    st = C.load_json(state)
    try:
        os.remove(state)
    except OSError:
        return                         # another copy of the hook got it first
    kind = row["agent_type"] if row["agent_type"] != "?" else C.dstr(st.get("type")) or "agent"
    cap = st.get("cap") if isinstance(st.get("cap"), int) else C.stop_cap()
    calls, total = row.get("calls") or 0, row.get("input_total") or 0
    if not calls:                      # no transcript path on the event: find the agent's own file
        try:
            import context_guard
            p = context_guard.agent_transcript(event, C.dstr(event.get("transcript_path")), sid, agent)
            calls, total, _, _ = totals(p) if p else (0, 0, 0, "")
        except Exception:
            pass
    handoff = C.dstr(st.get("handoff")) or C.handoff_path(kind, agent)
    there = "" if os.path.exists(handoff) else " (not written yet: ask it, or read its last message)"
    C.push_pending(sid, (
        f"🛑 {kind} {C.short_id(agent)} hit the {cap // 1000:,}k cap after {calls:,} calls (~{C.tok(total)} tokens "
        f"re-sent); handoff at {handoff}{there}; if the work isn't done, launch a fresh agent with: "
        f"read {handoff} and continue."))


def started(log, row):
    """True when a start row for the same (session, agent_id) is in the log's last 1 MB."""
    for line in C.tail_text(log, 1_000_000).splitlines():
        if '"start"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("event") == "start" and (d.get("agent_id"), d.get("session")) == (row["agent_id"], row["session"]):
            return True
    return False


def duplicate(log, row):
    """A row with the same (session, agent_id, event) written in the last 5 s is already there."""
    import time
    now = time.mktime(time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S"))
    for line in reversed(C.tail_text(log, 16_000).splitlines()):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or (d.get("agent_id"), d.get("event"), d.get("session")) != (row["agent_id"], row["event"], row["session"]):
            continue
        try:
            if now - time.mktime(time.strptime(C.dstr(d.get("ts")), "%Y-%m-%dT%H:%M:%S")) <= 5:
                return True
        except ValueError:
            return True
    return False


if __name__ == "__main__":
    main()
