#!/usr/bin/env python3
"""Big Dad hard stop for runaway subagents (PreToolUse: hooks/stop.sh checks the switch in sh and runs this only
when the stop is on; also standalone).

OFF by default. A warning alone did not stop two builder agents at 1.5x the guide, so this is the
opt-in next step: past the cap a SUBAGENT's tool calls are denied except Write/Edit (so it can still
write its handoff) and the hand-back. The deny reason names the handoff file
(~/.claude/bigdad/handoffs/<agent_type>-<short id>.md) and what goes in it; when the agent stops,
the agent monitor queues one relaunch line for the main chat. Never applies to the main chat.
`setup` proposes turning it on and explains the trade-off; Big Dad never turns it on by itself.

  BIGDAD_AGENT_STOP_AT=100k  cap every subagent at 100k tokens per call (absolute)
  BIGDAD_AGENT_STOP_AT=2     cap at 2x the per-call guide (0 / off = off; overrides the switch file)
  bigdad.py agent-stop 100k  the same, as a switch file
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

ALLOW = {"Write", "Edit", "MultiEdit", "NotebookEdit", "SubagentHandback"}      # handoff + report back still work
HANDOFF_PARTS = "goal, done so far, files touched + their state, the exact next step, open risks"


def stop_state(sid, agent):
    return os.path.join(C.state_dir(), f"bigdad-stop-{sid}-{agent}.json")


def main(event=None, standalone=True):
    event = C.read_event() if event is None else event
    if event is None:
        return
    agent = C.dstr(event.get("agent_id"))
    if not agent:
        return                          # never the main chat
    cap = C.stop_cap()
    if cap <= 0:
        return
    tool = C.dstr(event.get("tool_name"))
    if tool in ALLOW:
        return
    sid = C.dstr(event.get("session_id")) or "x"
    import context_guard
    path = context_guard.agent_transcript(event, C.dstr(event.get("transcript_path")), sid, agent)
    if not path:
        return
    tokens = C.last_call(path)[0]
    if not tokens or tokens < cap:
        return
    limit = C.guard_at()
    kind = C.dstr(event.get("agent_type"))
    handoff = C.handoff_path(kind, agent)
    state = stop_state(sid, agent)
    if not os.path.exists(state):       # log the first denial per agent, not every call
        try:
            with open(state, "w") as f:
                json.dump({"tokens": tokens, "at": C.now(), "cap": cap, "type": kind, "handoff": handoff}, f)
        except OSError:
            pass
        C.append_jsonl(os.path.join(C.bd_dir(), "agents.jsonl"), {
            "ts": C.now(), "event": "hard-stop", "session": sid[:8], "agent_id": agent[:17],
            "agent_type": kind, "tokens": tokens, "guard_at": limit, "stop_at": round(cap / limit, 2), "cap": cap})
    return C.emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"🥛 Big Dad 🛑 hard stop: this agent re-sends ~{tokens // 1000:,}k tokens per call, past its "
            f"{C.stop_label(cap)}. Tools other than Write/Edit are denied now; do not start new work. "
            f"Write your handoff to {handoff} ({HANDOFF_PARTS}), then hand back and name that path, "
            "so the parent can relaunch a fresh agent from it."),
    }}, standalone)


if __name__ == "__main__":
    main()
