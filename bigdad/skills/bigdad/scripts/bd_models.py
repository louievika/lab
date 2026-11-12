"""Big Dad `models`: tokens by model x execution mode, from Claude Code's own logs.

Modes: interactive, headless, subagent (with the agent type from its .meta.json when
there is one), fork, workflow. Flags the habits that cost most: more than one top-tier
model background agent at the same time (BIGDAD_BIG_MODEL, else the profile's big_model, else the
priciest family in the window), forks (they carry the whole chat), and agents spawned
without an explicit model (they run the parent's model, usually the costliest).
Counts only; never names or paths.
"""
import glob, json, os, re, time
from collections import defaultdict
from datetime import datetime, timezone
import bd_setup as S


def big_model(families):
    """The family the one-at-a-time rule is about: env, else profile, else the top-ranked family seen."""
    env = os.environ.get("BIGDAD_BIG_MODEL")
    if env:
        return env
    prof = S.H.load_json(S.PROFILE).get("big_model")
    if prof and prof != "?":
        return prof
    return max(families, key=S.rank) if families else ""


def short_model(m):
    mm = re.match(r"claude-([a-z]+)", m or "")
    return mm.group(1) if mm else (m or "?")


def meta_of(path):
    try:
        return json.load(open(path[:-6] + ".meta.json", encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def mode_of(path, entry, meta):
    s = path.replace("\\", "/")
    if "/workflows/" in s or meta.get("agentType") == "workflow-subagent":
        return "workflow"
    if meta.get("isFork") or meta.get("agentType") == "fork":
        return "fork"
    if "/subagents/" in s:
        return "subagent:" + (meta.get("agentType") or "?")
    return "headless" if entry and entry != "cli" else "interactive"


def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0


def scan(proj, since):
    """One pass over every log touched since `since`. Returns per-log facts."""
    out = []
    for p in glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
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
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") != "assistant":
                    continue
                msg = r.get("message") or {}
                u, mid = msg.get("usage") or {}, msg.get("id")
                if not u or mid in seen:
                    continue
                t = ts(r.get("timestamp") or "")
                if t and t < since:
                    continue
                seen.add(mid)
                n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                if not n:
                    continue
                row = per[short_model(msg.get("model"))]
                row[0] += 1; row[1] += n; row[2] += u.get("output_tokens") or 0
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
        print(f"  {model:<8} {mode:<26} {nl:>5}   {nc:>5}   {big(ni):>8}   {k(no):>6}   {k(ni // max(nc, 1)):>10}   {ni / total:>5.1%}")
    by_model = defaultdict(int)
    for (model, _), v in cell.items():
        by_model[model] += v[2]
    print("  by model: " + ", ".join(f"{m} {big(n)} ({n / total:.0%})" for m, n in sorted(by_model.items(), key=lambda kv: -kv[1])))
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
            kinds[L["mode"].split(":", 1)[1] + " on " + "/".join(sorted(L["per"]))] += 1
        print(f"  flag: {len(noexp)} agent(s) spawned without an explicit model ran a big model ({big(n)} input tokens): "
              + ", ".join(f"{t} x{c}" for t, c in sorted(kinds.items(), key=lambda kv: -kv[1])))
        print("        Set `model` on every Agent call (the cheapest tier for lookups); without it the agent inherits the parent's model.")
    BIG_MODEL = big_model({m for L in logs for m in L["per"]})
    bigs = [(L["first"], L["last"] or L["first"]) for L in subs
            if L["first"] and any(m == BIG_MODEL for m in L["per"]) and L["mode"] != "workflow"]
    if bigs:
        n, at = overlap(bigs)
        when = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if at else "?"
        if n > 1:
            print(f"  flag: up to {n} {BIG_MODEL} agents ran at the same time ({when}). Rule: one {BIG_MODEL} background agent at a time;")
            print("        the rest on cheaper models with short briefs.")
        else:
            print(f"  ok: never more than one {BIG_MODEL} agent at a time in this window ({len(bigs)} {BIG_MODEL} agent logs).")
    print("  input/call = what every call re-sends; the mode with the most input tokens is where cuts pay most.")
