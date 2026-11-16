"""Big Dad `models`: tokens by model x execution mode, from Claude Code's own logs.

Modes: interactive, headless, subagent (with the agent type from its .meta.json when
there is one), fork, workflow. Flags the habits that cost most: more than one top-tier
model background agent at the same time (BIGDAD_BIG_MODEL, else the profile's big_model, else the
priciest family in the window), forks (they carry the whole chat), and agents spawned
without an explicit model (they run the parent's model, usually the costliest).
Counts only; never names or paths.
"""
import glob, json, os, re, stat, sys, time
from collections import defaultdict
from datetime import datetime, timezone
import bd_setup as S
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
try:
    import bd_common as C  # noqa: E402  ODD_ROW, optional (bigdad.py's pattern)
except ImportError:        # seventeenth review: works without bd_common, as bigdad.py does
    C = None
ODD_ROW = getattr(C, "ODD_ROW", Exception)   # a log row that cannot be read (bd_common; missing: any error)


def big_model(families, profile=None):
    """The family the one-at-a-time rule is about: env, else profile, else the top-ranked family seen. profile: the
    profile already loaded (agents passes its own), else read here. Thirty-sixth review: a profile big_model that is
    not a non-empty str (5, a list) reads as unset, as a missing one; it turned the check off. agents uses this too."""
    env = os.environ.get("BIGDAD_BIG_MODEL")
    if env:
        return env
    p = profile if profile is not None else S.H.load_json(S.PROFILE)
    prof = p.get("big_model") if isinstance(p, dict) else None
    if isinstance(prof, str) and prof and prof != "?":
        return prof
    return max(families, key=S.rank) if families else ""


def as_shown(text):
    """A name from a log as safe to print, else '?' (agents and models use it): bd_common.label (thirty-seventh
    review: the shared rule, shown or '?'); '?' when bd_common is missing (this module loads without it, 17th review)."""
    return C.label(text) if C else "?"


def short_model(m):
    """The model family: bd_setup.family, the one rule (thirty-seventh review: was re.match, so a prefixed id such as
    us.anthropic.claude-opus-4-... stayed whole here and never matched the big model that agents found)."""
    return S.family(m)


def meta_of(path):
    """An agent log's .meta.json as a dict; {} when it is missing, unreadable or not an object (twenty-first review:
    a list, string or number crashed every meta.get; the file is closed). Twenty-third review: this one plain reader
    is used whether or not bd_common is there, never bd_common.load_json: that opens with open_wait, which can wait
    about 2.5 s on a .meta.json another program holds open on Windows; here a held file is {} at once. Twenty-fifth
    review: no blocking either: O_NONBLOCK, and only a regular file is read (a FIFO or a directory is {} at once), the
    same code as bd_common.load_json_now."""
    try:
        fd = os.open(path[:-6] + ".meta.json", os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except (OSError, ValueError, RecursionError):
        return {}
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:                              # a FIFO, a directory, a device: nothing to read here
        os.close(fd)
        return {}
    try:
        with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return d if isinstance(d, dict) else {}


def agent_type(meta):
    """meta's agentType when it is a str, else None (twenty-first review: a list or number is absent)."""
    t = meta.get("agentType")
    return t if isinstance(t, str) else None


def is_fork(meta):
    """True only when meta's isFork is the JSON boolean true (twenty-third review: "false", a list or 1 is not a fork)."""
    return meta.get("isFork") is True


def mode_of(path, entry, meta):
    s = path.replace("\\", "/")
    if "/workflows/" in s or agent_type(meta) == "workflow-subagent":
        return "workflow"
    if is_fork(meta) or agent_type(meta) == "fork":
        return "fork"
    if "/subagents/" in s:
        return "subagent:" + (agent_type(meta) or "?")
    return "headless" if entry and entry != "cli" else "interactive"


TS_MAX = 32503680000   # 3000-01-01T00:00:00Z (nineteenth review): times up to and including 3000-01-01T00:00:00Z
                       # itself are kept (t <= TS_MAX; twentieth review: said exactly), and a full
                       # year of room is left below 3000-12-31T23:59:59Z, the last second Windows can convert back
                       # (localtime, fromtimestamp), for arithmetic on a kept time (the chart pads its axis by 60 s)


def ts(s):
    """Epoch seconds of an ISO time, or 0 (no time) when it cannot be read or is before 1970 or after TS_MAX: every platform
    can convert what it returns back to a date (eighteenth review: a year-4000 or an aware 1969 time crashed later;
    nineteenth review: a time near the end of 3000 did too, once the chart added 60 s, so the rest of 3000 is out)."""
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, OSError, OverflowError):   # seventeenth review: a 1969 time on Windows,
        return 0                                                  # a huge year: no time
    return t if 0 <= t <= TS_MAX else 0                           # NaN fails both tests: 0 too


def _usage_row(line, since, seen):
    """One log line of scan, read whole before anything is counted: (id, time, model, input, output), or None when
    it is not a call to count. Raises (ODD_ROW) on a value of the wrong kind (a list "usage", a list "id"...).
    Twenty-first review: a row with no id (None) is never a duplicate; the caller never adds None to seen."""
    try:
        r = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(r, dict) or r.get("type") != "assistant":   # 3.0.0: a non-object line is skipped
        return None
    msg = r.get("message") if isinstance(r.get("message"), dict) else {}   # a list/string: no message
    u, mid = msg.get("usage") or {}, msg.get("id")
    if not u or (mid is not None and mid in seen):
        return None
    t = ts(r.get("timestamp") or "")
    if t and t < since:
        return None
    n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    o = u.get("output_tokens") or 0
    if not isinstance(o, (int, float)):        # sixteenth review: said, not a side effect of 0 + (a bool count
        raise TypeError("an output count that is not a number")   # passes, as 0 + True did); t is from ts()
    return mid, t, short_model(msg.get("model")), n, o


def scan(proj, since):
    """One pass over every log touched since `since`. Returns per-log facts."""
    out = []
    for p in glob.glob(os.path.join(glob.escape(proj), "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(p) < since:
                continue
        except OSError:
            continue
        entry, seen = None, set()
        per = defaultdict(lambda: [0, 0, 0])     # model -> calls, input, output
        first = last = 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if entry is None and '"entrypoint"' in line:
                    m = re.search(r'"entrypoint":\s*"([^"]+)"', line)
                    entry = m.group(1) if m else None
                if '"usage"' not in line:
                    continue
                try:
                    got = _usage_row(line, since, seen)
                except ODD_ROW:                     # 3.0.0 fifteenth review: a row that cannot be read: skipped whole
                    continue
                if got is None:
                    continue
                mid, t, model, n, o = got            # only now is anything counted
                if not n > 0:                        # fortieth review: above 0 (a negative or NaN sum was kept)
                    continue                         # thirty-ninth review: a row not kept never marks its id
                if mid is not None:                  # twenty-first review: an id-less row never hides a later one
                    seen.add(mid)
                row = per[model]
                row[0] += 1; row[1] += n; row[2] += o
                first, last = first or t, max(last, t)
        if not per:
            continue
        meta = meta_of(p) if "subagents" in p.replace("\\", "/") else {}
        out.append({"mode": mode_of(p, entry, meta), "per": dict(per), "meta": meta,
                    "first": first, "last": last, "sub": "/subagents/" in p.replace("\\", "/")})
    return out


def overlap(spans):
    """Largest number of spans alive at the same moment, and when."""
    ev = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
    cur = best = 0; at = 0
    for t, d in ev:
        cur += d
        if cur > best:
            best, at = cur, t
    return best, at


def k(n):
    return f"{n/1000:,.1f}k" if n >= 1000 else str(n)


def big(n):
    return f"{n/1e6:,.1f}M" if n >= 1e6 else k(n)


def cmd_models(proj, hours=24.0):
    since = time.time() - hours * 3600
    logs = scan(proj, since)
    cell = defaultdict(lambda: [0, 0, 0, 0])      # (model, mode) -> logs, calls, input, output
    for L in logs:
        for model, (c, i, o) in L["per"].items():
            x = cell[(model, L["mode"])]
            x[0] += 1; x[1] += c; x[2] += i; x[3] += o
    total = sum(v[2] for v in cell.values())
    print(f"🥛 == MODELS [claude] last {hours:g} h: {len(logs)} logs, {big(total)} input tokens by model x mode (verified: API usage)")
    if not total:
        print("  no usage in this window")
        return
    print("  model    mode                        logs   calls      input   output   input/call   share")
    for (model, mode), (nl, nc, ni, no) in sorted(cell.items(), key=lambda kv: -kv[1][2]):
        print(f"  {as_shown(model):<8} {as_shown(mode):<26} {nl:>5}   {nc:>5}   {big(ni):>8}   {k(no):>6}   {k(ni // max(nc, 1)):>10}   {ni / total:>5.1%}")
    by_model = defaultdict(int)
    for (model, _), v in cell.items():
        by_model[model] += v[2]
    print("  by model: " + ", ".join(f"{as_shown(m)} {big(n)} ({n / total:.0%})" for m, n in sorted(by_model.items(), key=lambda kv: -kv[1])))
    # flags
    subs = [L for L in logs if L["sub"]]
    forks = [L for L in logs if L["mode"] == "fork"]
    if forks:
        n = sum(sum(v[1] for v in L["per"].values()) for L in forks)
        print(f"  flag: {len(forks)} fork(s) used {big(n)} input tokens. A fork carries the whole chat on every call and runs the")
        print("        parent's model. For a long job, prefer a fresh agent with a short brief (file paths, not pasted text).")
    noexp = [L for L in subs if L["mode"].startswith("subagent:") and "model" not in L["meta"]
             and any(not S.is_small(m) for m in L["per"])]
    if noexp:
        n = sum(sum(v[1] for v in L["per"].values()) for L in noexp)
        kinds = defaultdict(int)
        for L in noexp:
            # thirty-sixth review: the agent type (from its .meta.json) through as_shown too, not only the models
            kinds[as_shown(L["mode"].split(":", 1)[1]) + " on " + "/".join(as_shown(m) for m in sorted(L["per"]))] += 1
        print(f"  flag: {len(noexp)} agent(s) spawned without an explicit model ran a big model ({big(n)} input tokens): "
              + ", ".join(f"{t} x{c}" for t, c in sorted(kinds.items(), key=lambda kv: -kv[1])))
        print("        Set `model` on every Agent call (the cheapest tier for lookups); without it the agent inherits the parent's model.")
    BIG_MODEL = big_model({m for L in logs for m in L["per"]})
    BIG_SHOWN = as_shown(BIG_MODEL)  # thirty-fifth review: printed safely; `m == BIG_MODEL` keeps the raw value
    bigs = [(L["first"], L["last"] or L["first"]) for L in subs
            if L["first"] and any(m == BIG_MODEL for m in L["per"]) and L["mode"] != "workflow"]
    if bigs:
        n, at = overlap(bigs)
        when = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if at else "?"
        if n > 1:
            print(f"  flag: up to {n} {BIG_SHOWN} agents ran at the same time ({when}). Rule: one {BIG_SHOWN} background agent at a time;")
            print("        the rest on cheaper models with short briefs.")
        else:
            print(f"  ok: never more than one {BIG_SHOWN} agent at a time in this window ({len(bigs)} {BIG_SHOWN} agent logs).")
    print("  input/call = what every call re-sends; the mode with the most input tokens is where cuts pay most.")
