"""Big Dad `agents`: every subagent the monitor hook saw, running and finished.

Reads ~/.claude/bigdad/agents.jsonl (start/stop/over-guard rows from hooks/agent_monitor.py and
the context guard) and, for agents still running, their own transcript under
<project>/<session>/subagents/ for the size now. Flags: more than max_big_agents on the
priciest model tier at once, an agent past the per-call guide, a fork carrying a big chat,
an agent spawned with no explicit model. Counts and short ids only.
"""
import glob, json, math, os, time
from collections import defaultdict
import bd_health as H
import bd_setup as S
from bd_models import agent_type, as_shown, big, big_model as pick_big, is_fork, k, meta_of, ts   # one copy of each helper

LOG = os.environ.get("BIGDAD_AGENTS_LOG") or os.path.join(H.CLAUDE_DIR, "bigdad", "agents.jsonl")


def plain_id(aid):
    """True when an agent id counts (twenty-seventh review): a str that is not empty, "." or "..", with no "/", "\\" or
    NUL. The hooks only read a number id as empty (bd_common.dstr); the rest of this rule is `agents`' own. A number
    would prefix-match real hex ids; a path separator could lead transcript_of's pattern out of the subagents folder."""
    return isinstance(aid, str) and aid not in ("", ".", "..") and not any(c in aid for c in "/\\\0")


NUM_FIELDS = ("calls", "input_total", "last_call", "tokens")   # every row field cmd_agents adds, compares or formats
TEXT_FIELDS = ("agent_type", "model")                         # every row field it slices, formats or passes on as text


def _num(v, is_number):
    """v when it is a real finite number (int or float, not bool), else 0. is_number: bd_common.is_number, bound once
    by the caller (twenty-ninth review)."""
    try:
        return v if is_number(v) and math.isfinite(v) else 0
    except OverflowError:                        # an int too big for a float
        return 0


def rows():
    """The rows of agents.jsonl with a plain_id agent_id and a usable time; a number field that is not a real finite
    number reads as 0 and a text field that is not a str as "" (twenty-seventh review: read once here, not checked at
    each use). "ts" is read once here into epoch seconds; a row whose ts gives no usable time (ts(...) == 0) is left
    out whatever the window (twenty-ninth review)."""
    C = hooks_common()
    is_number = C.is_number
    out = []
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(d, dict) or not plain_id(d.get("agent_id")):
                    continue
                d["ts"] = ts(d.get("ts", ""))
                if not d["ts"]:
                    continue
                for key in NUM_FIELDS:
                    if key in d:
                        d[key] = _num(d[key], is_number)
                for key in TEXT_FIELDS:
                    if key in d:
                        d[key] = C.dstr(d[key])
                out.append(d)
    except OSError:
        pass
    return out


def agent_logs(name="agent-*.jsonl"):
    """Every agent log under every project: <session>/subagents/ and, for workflow helpers, one folder deeper at
    <session>/subagents/workflows/<run>/ (Claude Code's own layout)."""
    base = os.path.join(glob.escape(H.CLAUDE_DIR), "projects", "*", "*", "subagents")   # twenty-fifth review: as bd_money
    return glob.glob(os.path.join(base, name)) + glob.glob(os.path.join(base, "workflows", "*", name))


def transcript_of(agent_id):
    if not plain_id(agent_id):                   # twenty-seventh review: rows()'s rule, for a direct caller too
        return ""
    hits = agent_logs(f"agent-{glob.escape(agent_id)}*.jsonl")
    return hits[0] if hits else ""


def session_of(path):
    """The session folder an agent log belongs to: the one that holds `subagents`, however deep the log sits."""
    parts = os.path.normpath(path).split(os.sep)
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "subagents":
            return parts[i - 1]
    return os.path.basename(os.path.dirname(os.path.dirname(path)))



HOOKS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))


_MONITOR = None                                  # twenty-sixth review: the monitor hook, loaded once per process


def _monitor():
    """The monitor hook's module, loaded from source on first use and kept; a failed load is not kept (None)."""
    global _MONITOR
    if _MONITOR is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_monitor", os.path.join(HOOKS_DIR, "agent_monitor.py"))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            return None
        _MONITOR = mod
    return _MONITOR


def live_totals(path):
    """(calls, total input, last call, model) from a running agent's own transcript, via the monitor hook's reader."""
    mod = _monitor()
    if mod is None:
        return 0, 0, 0, ""
    try:
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
                except (ValueError, AttributeError, RecursionError):
                    continue
                n = sum(u.get(key) or 0 for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                        if isinstance(u.get(key), int)) if isinstance(u, dict) else 0
                if n:
                    return n
    except OSError:
        pass
    return 0


# 3.0.0: the host's own section headers in an agent's system blob, in the order Claude Code writes them; each part runs
# to the next header found. Checked against real agent logs (row types and key names only, 2026-11-14).
HOST_PARTS = (("deferred tool names", "The following deferred tools are now available"),
              ("MCP server instructions", "# MCP Server Instructions"),
              ("agent types", "Available agent types"),
              ("environment", "# Environment"),
              ("skill list", "The following skills are available"))


def _texts(content):
    if isinstance(content, str):
        return [content]
    return [c.get("text") for c in content or [] if isinstance(c, dict) and isinstance(c.get("text"), str)]


def start_parts(path):
    """Chars per part of an agent's fixed start, read from its log up to its first usage row: the system prompt and
    tool list (api-request-shape row), the host's system blob split at its own headers (HOST_PARTS; deferred names
    also counted per mcp__<server>__), CLAUDE.md and memory from the user blob's system-reminders. The brief is left
    out. Returns (parts, servers); both empty when the log has no request rows."""
    parts, servers = defaultdict(int), defaultdict(int)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(r, dict):
                    continue
                msg = r.get("message") if isinstance(r.get("message"), dict) else {}
                if msg.get("usage"):
                    break
                if r.get("type") == "api-request-shape" and isinstance(r.get("shape"), dict):
                    shape = r["shape"]
                    parts["system prompt"] += sum(len(t) for t in _texts(shape.get("system")))
                    if isinstance(shape.get("tools"), list):
                        parts["tool list"] += len(json.dumps(shape["tools"]))
                elif r.get("type") == "api-request-blob" and msg.get("role") == "system":
                    for t in _texts(msg.get("content")):
                        at = sorted((t.find(mark), name) for name, mark in HOST_PARTS if mark in t)
                        for i, (start, name) in enumerate(at):
                            end = at[i + 1][0] if i + 1 < len(at) else len(t)
                            parts[name] += end - start
                            if name == "deferred tool names":
                                for ln in t[start:end].splitlines():
                                    if ln.startswith("mcp__") and "__" in ln[5:]:
                                        servers[ln[5:].split("__", 1)[0]] += 1
                elif r.get("type") == "api-request-blob" and msg.get("role") == "user":
                    for t in _texts(msg.get("content")):
                        if not t.lstrip().startswith("<system-reminder>") or "Contents of " not in t:
                            continue           # the brief, and reminders that are not a memory file
                        chunks = t.split("Contents of ")
                        parts["CLAUDE.md"] += len(chunks[0])
                        for c in chunks[1:]:
                            head = c.split("\n", 1)[0]
                            parts["memory" if "MEMORY.md" in head or "memory" in head.lower() else "CLAUDE.md"] += len(c) + 12
    except OSError:
        pass
    return {n: c for n, c in parts.items() if c}, dict(servers)


def biggest_parts(paths, top=3):
    """'Biggest parts here' line from the first log that has request rows; '' when none has them."""
    for path in filter(None, paths):
        parts, servers = start_parts(path)
        if not parts:
            continue
        named = []
        for name, chars in sorted(parts.items(), key=lambda x: -x[1])[:top]:
            label = f"{name} ~{k(chars // 4)}"
            if name == "deferred tool names" and servers:
                srv, n = max(servers.items(), key=lambda x: x[1])
                label += f" (most: mcp__{srv}__, {n})"
            named.append(label)
        return ("Biggest parts here (est., chars / 4, from one agent's log): " + ", ".join(named)
                + "; yours to keep or trim.")
    return ""


_COMMON = None                                   # twenty-eighth review: the hooks' bd_common, resolved once per process


def hooks_common():
    global _COMMON
    if _COMMON is None:
        import sys
        if HOOKS_DIR not in sys.path:
            sys.path.insert(0, HOOKS_DIR)
        import bd_common
        _COMMON = bd_common
    return _COMMON


def guard_at():
    """The per-call guide the context guard hook uses (BIGDAD_GUARD_AT, else the profile, else 130k), so "past
    guard" here means what the hook warned about."""
    return hooks_common().guard_at()


START_CALLS = 20     # the fixed-start line needs at least this many calls across the agents it counts
START_SHARE = 0.25    # and the fixed start must be at least this share of their total


def cmd_agents(hours=24.0, profile=None, table=None):
    """Print the agents report. table: an optional list that gets one dict per printed agent row (state, type, model,
    calls, last, total: the printed text of each column; flags: a list), for bd_report."""
    p = profile or S.load() or {}
    max_big = S._bc().sane(p.get("max_big_agents"), 0, whole=True) or 1   # thirty-ninth review: not a count: 1
    guard = guard_at()
    since = time.time() - hours * 3600
    agents = {}
    for r in rows():
        if r["ts"] < since:                      # epoch seconds, never 0 (rows())
            continue
        if r.get("event") == "stop" and (r.get("agent_type") or "?") == "?" and r["agent_id"] not in agents:
            continue                             # phantom stop (no start, no type): headless --resume fires these; not an agent
        a = agents.setdefault(r["agent_id"], {"type": r.get("agent_type") or "?", "start": 0, "stop": 0, "over": 0, "calls": 0, "total": 0, "last": 0, "model": ""})
        a["type"] = r.get("agent_type") or a["type"]
        ev = r.get("event")
        if ev == "start":
            a["start"] = r["ts"]
        elif ev == "stop":
            a["stop"] = r["ts"]
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
        if mt < since or any(aid.startswith(known) or known.startswith(aid) for known in agents):
            continue
        meta = meta_of(path)                     # read once: kept on the agent below and reused (twenty-fifth review)
        c, t, l, m = live_totals(path)
        first = mt                               # start = the first timestamp in its own log (overlap checks need it)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:                   # nineteenth review: a line that is not an object is skipped, the
                    try:                         # search goes on (a list or string line ended it before)
                        r = json.loads(line) if line.strip() else None
                        v = ts(r.get("timestamp", "")) if isinstance(r, dict) else 0
                    except (ValueError, RecursionError):     # twentieth review: parse errors only (ts never raises)
                        continue
                    if v:
                        first = v
                        break
        except OSError:                          # twentieth review: the file read only
            pass
        agents[aid] = {"type": agent_type(meta) or "?", "session": session_of(path)[:8],
                       "start": first, "stop": mt if time.time() - mt > 600 else 0, "over": 0, "calls": c, "total": t, "last": l,
                       "model": m, "unlogged": True, "first": first_call(path), "path": path, "meta": meta}
    if not agents:
        print(f"🥛 == AGENTS  none logged in the last {hours:g} h (the SubagentStart/Stop hooks log each agent to agents.jsonl)")
        return
    running = []
    C = hooks_common()

    def log_mtime(path):                         # a stopped agent's stop time: its log's mtime, else now
        try:
            return os.path.getmtime(path)
        except OSError:
            return time.time()
    for aid, a in agents.items():
        if "path" not in a:                      # an unlogged agent's own log is already found
            a["path"] = transcript_of(aid)       # kept: biggest_parts below reuses it
        path = a["path"]
        meta = a["meta"] if "meta" in a else (meta_of(path) if path else {})
        a["explicit_model"] = "model" in meta if meta else None    # twenty-fourth review: running as stopped
        a["fork"] = is_fork(meta) or agent_type(meta) == "fork"
        if not a["stop"]:
            q = C.last_quit(path) if path else None   # 3.0.0: a failed call ends an agent with no stop row
            if q:
                a["quit"] = q
                a["stop"] = log_mtime(path)
            if path:
                if not a.get("unlogged"):        # an unlogged agent's totals and first call were read above
                    c, t, l, m = live_totals(path)
                    a.update(calls=c, total=t, last=l, model=m or a["model"])
                    a["first"] = first_call(path)
                if meta.get("finished") is True and not a["stop"]:   # twenty-fourth review: the JSON boolean only
                    a["stop"] = log_mtime(path)  # twenty-fifth review: its log's mtime, as a quit agent's, not now
            if not a["stop"]:
                running.append(aid)
        else:
            a["quit"] = a.get("quit") or (C.last_quit(path) if path else None)
            a["first"] = a.get("first") or (first_call(path) if path else 0)
    # rows with nothing measured and no type (a transcript already cleaned up): count them, don't list them
    unmeasured = [aid for aid, a in agents.items() if not a["calls"] and not a["last"] and a["type"] == "?" and aid not in running]
    for aid in unmeasured:
        del agents[aid]
    # thirty-sixth review: models' rule (bd_models.big_model): BIGDAD_BIG_MODEL, else a profile big_model that is a
    # non-empty str (5 or a list turned the check off), else the top family among these agents
    big_model = pick_big({S.family(a["model"]) for a in agents.values() if a["model"]}, p)
    # thirty-fifth review: printed safely (as_shown); the spans below compare the raw value
    shown_big = as_shown(big_model)
    print(f"🥛 == AGENTS  last {hours:g} h: {len(agents)} agents, {len(running)} running (big model = {shown_big}, guard {guard // 1000}k/call)"
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
        state = "running" if aid in running else "stopped" if a.get("quit") else "done"
        if a.get("quit"):                        # thirty-sixth review: a limit name read from the log, printed safely
            fl.append("quit: " + C.shown(C.quit_what(a["quit"])))
        # thirty-fifth review: the model column printed safely too (it is the big-model text when they match);
        # thirty-sixth review: the type column as well
        row = {"state": state, "type": as_shown(a["type"][:20]), "model": as_shown(S.family(a["model"])) if a["model"] else "?",
               "calls": f"{a['calls']}", "last": k(a["last"]), "total": big(a["total"]), "flags": fl}
        if table is not None:                    # twenty-ninth review: the report reads these, not the printed text
            table.append(row)
        print(f"  {row['state']:<9} {row['type']:<20} {row['model']:<9} {row['calls']:>5}   {row['last']:>9}   {row['total']:>8}   {', '.join(row['flags'])}")
    spans = [(a["start"] or a["stop"], a["stop"] or time.time()) for a in agents.values()
             if big_model and S.family(a["model"]) == big_model and (a["start"] or a["stop"])]
    if spans:
        ev = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
        cur = best = 0
        for _, d in ev:
            cur += d
            best = max(best, cur)
        if best > max_big:
            print(f"  flag: up to {best} {shown_big} agents ran at the same time (default max {max_big}; BIGDAD_MAX_BIG_AGENTS to change).")
            print("        Keep one big-model background agent at a time; the rest on a cheaper model with short briefs.")
        else:
            print(f"  ok: never more than {max_big} {shown_big} agent(s) at a time.")
    for f, n in flags_all.items():
        print(f"  flag: {n} agent(s) {f}.")
    if flags_all.get("past guard"):
        print("        Fix, native first: `autoCompactWindow` in that agent's frontmatter (restart open sessions to apply);"
              " fallback: `bigdad.py agent-stop 2`.")
    quits = {}
    for aid, a in agents.items():
        q = a.get("quit")
        if q:
            quits.setdefault((q.get("kind"), q.get("limit"), q.get("resets_at")), [q, []])[1].append((a["type"], aid))
    for q, who in quits.values():                # thirty-sixth review: agent types, ids and the limit name printed safely
        print("  " + C.shown(C.quit_line(who, q)))
    counted = [a for a in agents.values() if a.get("first") and a["calls"] >= 2 and a["total"]]
    calls = sum(a["calls"] for a in counted)
    fixed = sum(min(a["first"] * a["calls"], a["total"]) for a in counted)
    spent = sum(a["total"] for a in counted)
    if calls >= START_CALLS and spent and fixed / spent >= START_SHARE:
        lo, hi = min(a["first"] for a in counted), max(a["first"] for a in counted)
        size = k(lo) if lo == hi else f"{k(lo)}-{k(hi)}"
        print(f"  fixed start: {len(counted)} agent(s) sent their first-call size ({size}) again on each of {calls} calls"
              f" = {big(fixed)} of {big(spent)} ({fixed / spent:.0%}; estimated: first call x calls).")
        print("        Claude Code itself sends this part on every call (system prompt, skill list, tool list, CLAUDE.md,"
              " memory index); Big Dad does not control it.")
        parts = biggest_parts(a["path"] for a in counted)   # counted keeps agents' order
        if parts:                                # thirty-sixth review: an MCP server name read from a log, printed safely
            print("        " + C.shown(parts))
        print("        Lever: fewer, bigger steps (one script per step, not one command per call) and a short brief.")
    note = S.window_note(p)
    if note:
        print("  " + note)
    total = sum(a["total"] for a in agents.values())
    print(f"  total input tokens across these agents: {big(total)} (verified: API usage in each agent's own transcript)")
