"""Big Dad `agents`: every subagent the monitor hook saw, running and finished.

Reads ~/.claude/bigdad/agents.jsonl (start/stop/over-guard rows from hooks/agent_monitor.py and
the context guard) and, for agents still running, their own transcript under
<project>/<session>/subagents/ for the size now. Flags: more than max_big_agents on the
priciest model tier at once, an agent past the per-call guide, a fork carrying a big chat,
an agent spawned with no explicit model. Counts and short ids only.
"""
import glob, json, os, time
from collections import defaultdict
import bd_health as H
import bd_setup as S
from bd_models import big, k, meta_of, ts   # one copy of each helper

LOG = os.environ.get("BIGDAD_AGENTS_LOG") or os.path.join(H.CLAUDE_DIR, "bigdad", "agents.jsonl")


def rows():
    out = []
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("agent_id"):
                    out.append(d)
    except OSError:
        pass
    return out


def agent_logs(name="agent-*.jsonl"):
    """Every agent log under every project: <session>/subagents/ and, for workflow helpers, one folder deeper at
    <session>/subagents/workflows/<run>/ (Claude Code's own layout)."""
    base = os.path.join(H.CLAUDE_DIR, "projects", "*", "*", "subagents")
    return glob.glob(os.path.join(base, name)) + glob.glob(os.path.join(base, "workflows", "*", name))


def transcript_of(agent_id):
    hits = agent_logs(f"agent-{agent_id}*.jsonl")
    return hits[0] if hits else ""


def session_of(path):
    """The session folder an agent log belongs to: the one that holds `subagents`, however deep the log sits."""
    parts = os.path.normpath(path).split(os.sep)
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "subagents":
            return parts[i - 1]
    return os.path.basename(os.path.dirname(os.path.dirname(path)))



HOOKS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))


def live_totals(path):
    """(calls, total input, last call, model) from a running agent's own transcript, via the monitor hook's reader."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_monitor", os.path.join(HOOKS_DIR, "agent_monitor.py"))
    if spec is None or spec.loader is None:
        return 0, 0, 0, ""
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod.totals(path)
    except Exception:
        return 0, 0, 0, ""



def first_call(path):
    """Input size of an agent's first call: its fixed start (system text, tools, memory files, the brief).
    Every later call sends it again. 0 when the log has no call yet."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    u = ((json.loads(line) or {}).get("message") or {}).get("usage") or {}
                except (ValueError, AttributeError):
                    continue
                n = sum(u.get(key) or 0 for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                        if isinstance(u.get(key), int)) if isinstance(u, dict) else 0
                if n:
                    return n
    except OSError:
        pass
    return 0


def guard_at():
    """The per-call guide the context guard hook uses (BIGDAD_GUARD_AT, else the profile, else 130k), so "past
    guard" here means what the hook warned about."""
    import sys
    if HOOKS_DIR not in sys.path:
        sys.path.insert(0, HOOKS_DIR)
    import bd_common
    return bd_common.guard_at()


START_CALLS = 20     # the fixed-start line needs at least this many calls across the agents it counts
START_SHARE = 0.25    # and the fixed start must be at least this share of their total


def cmd_agents(hours=24.0, profile=None):
    p = profile or S.load() or {}
    big_model = os.environ.get("BIGDAD_BIG_MODEL") or p.get("big_model") or ""
    max_big = p.get("max_big_agents") or 1
    guard = guard_at()
    since = time.time() - hours * 3600
    agents = {}
    for r in rows():
        if ts(r.get("ts", "")) < since:
            continue
        if r.get("event") == "stop" and (r.get("agent_type") or "?") == "?" and r["agent_id"] not in agents:
            continue                             # phantom stop (no start, no type): headless --resume fires these; not an agent
        a = agents.setdefault(r["agent_id"], {"type": r.get("agent_type") or "?", "session": r.get("session", "?"),
                                              "start": 0, "stop": 0, "over": 0, "calls": 0, "total": 0, "last": 0, "model": ""})
        a["type"] = r.get("agent_type") or a["type"]
        ev = r.get("event")
        if ev == "start":
            a["start"] = ts(r["ts"])
        elif ev == "stop":
            a["stop"] = ts(r["ts"])
            a.update(calls=r.get("calls", 0), total=r.get("input_total", 0), last=r.get("last_call", 0), model=r.get("model") or a["model"])
        elif ev == "over-guard":
            a["over"] = max(a["over"], r.get("tokens", 0))
    # agents the hooks never logged (a session that started before Big Dad's hooks loaded, or hooks off):
    # read them from their own logs in every project, so a runaway helper can't hide from the report
    for path in agent_logs():
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        aid = os.path.basename(path)[6:-6]
        if mt < since or any(aid.startswith(k) or k.startswith(aid) for k in agents):
            continue
        meta = meta_of(path)
        c, t, l, m = live_totals(path)
        first = mt                               # start = the first timestamp in its own log (overlap checks need it)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    v = ts((json.loads(line) or {}).get("timestamp", "")) if line.strip() else 0
                    if v:
                        first = v
                        break
        except (OSError, ValueError, AttributeError):
            pass
        agents[aid] = {"type": meta.get("agentType") or "?", "session": session_of(path)[:8],
                       "start": first, "stop": mt if time.time() - mt > 600 else 0, "over": 0, "calls": c, "total": t, "last": l,
                       "model": m, "unlogged": True, "first": first_call(path)}
    if not agents:
        print(f"🥛 == AGENTS  none logged in the last {hours:g} h (the SubagentStart/Stop hooks log each agent to agents.jsonl)")
        return
    running = []
    for aid, a in agents.items():
        if not a["stop"]:
            path = transcript_of(aid)
            if path:
                c, t, l, m = live_totals(path)
                a.update(calls=c, total=t, last=l, model=m or a["model"])
                meta = meta_of(path)
                a["explicit_model"] = "model" in meta
                a["fork"] = bool(meta.get("isFork")) or meta.get("agentType") == "fork"
                a["first"] = first_call(path)
                if meta.get("finished"):
                    a["stop"] = a["stop"] or time.time()
            if not a["stop"]:
                running.append(aid)
        else:
            path = transcript_of(aid)
            meta = meta_of(path) if path else {}
            a["explicit_model"] = "model" in meta if meta else None
            a["fork"] = bool(meta.get("isFork")) or meta.get("agentType") == "fork"
            a["first"] = a.get("first") or (first_call(path) if path else 0)
    # rows with nothing measured and no type (a transcript already cleaned up): count them, don't list them
    unmeasured = [aid for aid, a in agents.items() if not a["calls"] and not a["last"] and a["type"] == "?" and aid not in running]
    for aid in unmeasured:
        del agents[aid]
    print(f"🥛 == AGENTS  last {hours:g} h: {len(agents)} agents, {len(running)} running (big model = {big_model or '?'}, guard {guard // 1000}k/call)"
          + (f"; {len(unmeasured)} more with no transcript left to measure" if unmeasured else ""))
    if not agents:
        return
    print("  state     type                 model     calls   last call      total   flags")
    flags_all = defaultdict(int)
    for aid, a in sorted(agents.items(), key=lambda kv: -(kv[1]["start"] or kv[1]["stop"])):
        fl = []
        if a["last"] >= guard or a["over"] >= guard:     # judged by this guide, not a test run's lower one
            fl.append("past guard")
        if a.get("fork") and a["last"] >= 100_000:
            fl.append("fork >100k/call")
        if a.get("explicit_model") is False and not S.is_small(a["model"]):
            fl.append("no explicit model")
        for f in fl:
            flags_all[f] += 1
        state = "running" if aid in running else "done"
        print(f"  {state:<9} {a['type'][:20]:<20} {S.family(a['model']) if a['model'] else '?':<9} {a['calls']:>5}   {k(a['last']):>9}   {big(a['total']):>8}   {', '.join(fl)}")
    spans = [(a["start"] or a["stop"], a["stop"] or time.time()) for a in agents.values()
             if big_model and S.family(a["model"]) == big_model and (a["start"] or a["stop"])]
    if spans:
        ev = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
        cur = best = 0
        for _, d in ev:
            cur += d
            best = max(best, cur)
        if best > max_big:
            print(f"  flag: up to {best} {big_model} agents ran at the same time (default max {max_big}; BIGDAD_MAX_BIG_AGENTS to change).")
            print("        Keep one big-model background agent at a time; the rest on a cheaper model with short briefs.")
        else:
            print(f"  ok: never more than {max_big} {big_model} agent(s) at a time.")
    for f, n in flags_all.items():
        print(f"  flag: {n} agent(s) {f}.")
    if flags_all.get("past guard"):
        print("        Fix, native first: `autoCompactWindow` in that agent's frontmatter (restart open sessions to apply);"
              " fallback: `bigdad.py agent-stop 2`.")
    counted = [a for a in agents.values() if a.get("first") and a["calls"] >= 2 and a["total"]]
    calls = sum(a["calls"] for a in counted)
    fixed = sum(min(a["first"] * a["calls"], a["total"]) for a in counted)
    spent = sum(a["total"] for a in counted)
    if calls >= START_CALLS and spent and fixed / spent >= START_SHARE:
        lo, hi = min(a["first"] for a in counted), max(a["first"] for a in counted)
        size = k(lo) if lo == hi else f"{k(lo)}-{k(hi)}"
        print(f"  fixed start: {len(counted)} agent(s) sent their first-call size ({size}) again on each of {calls} calls"
              f" = {big(fixed)} of {big(spent)} ({fixed / spent:.0%}; estimated: first call x calls).")
        print("        Lever: fewer, bigger steps (one script per step, not one command per call) and a short brief.")
    note = S.window_note(p)
    if note:
        print("  " + note)
    total = sum(a["total"] for a in agents.values())
    print(f"  total input tokens across these agents: {big(total)} (verified: API usage in each agent's own transcript)")
