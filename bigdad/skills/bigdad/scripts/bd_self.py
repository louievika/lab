"""`bigdad.py self [DAYS]`: what Big Dad itself costs vs what it saved, per part, from Claude Code's own logs.

Cost = every token of Big Dad text that went into the model's context (its skill-list line, hook notes, deny reasons,
the skill page when invoked) times the calls that re-sent it: until the next compact for chat text, every later call
for the skill-list line. Saved = what a cut kept out of later calls: a read-guard deny (the blocked copy times the
calls left before the next compact; not credited when the model read the file again within 3 calls) and output
shrink (tokens cut times calls left). Text is ≈4 characters per token. Read-only; proposes, never switches anything.
"""
import glob, json, os, re, time
from collections import defaultdict

BD = re.compile(r"big ?dad|bigdad|\U0001f95b", re.I)
SHRINK = re.compile(r"Big Dad shrink: showed [\d,]+ of [\d,]+ lines \(~([\d,]+) → ~([\d,]+) tokens")
READ_DENY = ("in your context", "already in context", "is still in your context")
OUTLINE = "Outline first, so the whole file"

# part -> how to switch it off (proposal text). None = no switch: the fix is a smaller text in Big Dad itself.
SWITCH = {
    "skill-list line": None,
    "skill page (/bigdad)": None,
    "startup note": None,
    "read guard": "env BIGDAD_READ_GUARD=off in settings.json",
    "big-file note": "bigdad.py outline off",
    "outline-first": "bigdad.py outline off",
    "context guard": "env BIGDAD_GUARD=off in settings.json",
    "redirect note": "env BIGDAD_REDIRECT=off in settings.json",
    "loop note": "env BIGDAD_LOOP=off in settings.json",
    "watch": "env BIGDAD_WATCH=off in settings.json",
    "agent guard": "env BIGDAD_AGENTS=off in settings.json",
    "output shrink": "bigdad.py shrink off",
    "other hook text": None,
}


# thirty-eighth review: a log field of the wrong type reads as missing (a str, a dict, a list, else empty / None) and
# the row still counts where it can; bd_self loads without bd_common, so these are its own small readers
def _s(v):
    return v if isinstance(v, str) else ""


def _d(v):
    return v if isinstance(v, dict) else {}


def _l(v):
    return v if isinstance(v, list) else []


def _id(v):
    return v if isinstance(v, str) and v else None


def _sent(u):
    """A usage's input counts summed, each an int (not bool) up to 10**12, else read as 0 (thirty-ninth review).
    Fortieth review: a negative count (input or output) makes it no call (0), the receipt's Tally.add rule; a
    negative was read as 0 and the row still counted."""
    u = _d(u)
    raw = [u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")]
    if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0 for v in raw):
        return 0
    return sum(v for v in raw[:3] if type(v) is int and 0 <= v <= 10 ** 12)


def tok(s):
    return len(s) // 4 if s else 0


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(_s(x.get("text")) for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def part_of(event, text):
    """Which Big Dad part wrote a hook note, from its hook event and its words."""
    low = text.lower()
    if event == "SessionStart":
        return "startup note"
    if event == "UserPromptSubmit":
        return "redirect note"
    if event in ("SubagentStart", "SubagentStop") or "agent" in low and ("relaunch" in low or "handoff" in low):
        return "agent guard"
    if "same call" in low or "same input" in low or "loop" in low:
        return "loop note"
    if "watch" in low and "noted" in low:
        return "watch"
    if event == "PreToolUse" and ("grep" in low or "rides along" in low):
        return "big-file note"
    if "guide" in low or "re-sends" in low or "/compact" in low:
        return "context guard"
    return "other hook text"


def listing_line(content):
    """Big Dad's own line in a skill_listing attachment, or None."""
    for line in _s(content).splitlines():
        if re.match(r"\s*-\s*(bigdad:)?bigdad:", line):
            return line
    return None


def scan(path, since=""):
    """Ordered events of one log: call, compact, listing(tokens), inject(part, tokens), read(fp),
    block(fp, blocked_tokens, deny_tokens), shrink(cut), outline."""
    ev, seen_msg, seen_res, reads, last, cwds = [], set(), set(), {}, {}, set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return ev
    with fh:
        for ln in fh:
            try:
                e = json.loads(ln)
            except (ValueError, RecursionError):
                continue
            if not isinstance(e, dict):
                continue
            if since and _s(e.get("timestamp")) < since:   # before this version: not its cost
                continue
            cwd = e.get("cwd")
            if isinstance(cwd, str) and cwd not in cwds:
                cwds.add(cwd)
                ev.append(("cwd", cwd))
            t = e.get("type")
            if t == "api-request-blob":                      # a copy of rows already logged
                continue
            if t == "system" and e.get("subtype") == "compact_boundary":
                ev.append(("compact",))
                continue
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            if t == "assistant":
                mid = _id(m.get("id"))
                # thirty-ninth review: a call counts (and marks its id) only when its input counts are above 0, the
                # rule of walk, modes and the receipt; a damaged first block no longer stands for the call
                if mid and mid not in seen_msg and _sent(m.get("usage")):
                    seen_msg.add(mid)
                    ev.append(("call",))
                for b in _l(m.get("content")):
                    if isinstance(b, dict) and b.get("type") == "tool_use" and DEV.search(json.dumps(b.get("input"))):
                        ev.append(("touch",))
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read":
                        fp = _s(_d(b.get("input")).get("file_path"))
                        rid = _id(b.get("id"))
                        # thirty-ninth review: an id-less Read is still a read event, but is never stored under
                        # None (every id-less Read shared that one key: later ones were dropped)
                        if rid is None or rid not in reads:
                            if rid is not None:
                                reads[rid] = fp
                            ev.append(("read", fp))
                continue
            if t == "attachment":
                a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
                at = _s(a.get("type"))
                if at == "skill_listing":
                    line = listing_line(a.get("content"))
                    if line:
                        ev.append(("listing", tok(line)))
                elif at.startswith("hook_") and at != "hook_system_message":   # systemMessage: shown, not sent
                    c = a.get("content")
                    c = c if isinstance(c, str) else " ".join(map(str, c)) if isinstance(c, list) else \
                        json.dumps(c, ensure_ascii=False) if c else ""
                    if c and BD.search(c):
                        ev.append(("inject", part_of(_s(a.get("hookEvent")), c), tok(c)))
                continue
            if t != "user":
                continue
            body = m.get("content")
            if not isinstance(body, list):
                continue
            for b in body:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    s = _s(b.get("text"))
                    if "Base directory for this skill" in s[:400] and "bigdad" in s[:400].lower():
                        ev.append(("inject", "skill page (/bigdad)", tok(s)))
                    continue
                if b.get("type") != "tool_result":
                    continue
                tid = _id(b.get("tool_use_id"))
                if tid and tid in seen_res:
                    continue
                if tid:
                    seen_res.add(tid)
                c = text_of(b.get("content"))
                fp = reads.get(tid) if tid else None   # thirty-ninth review: an id-less result matches no Read
                if b.get("is_error") and "Big Dad" in c[:120] and not c.startswith("Exit code"):   # Big Dad's own deny,
                    # not a failed command whose output happens to mention Big Dad
                    if OUTLINE in c:
                        ev.append(("inject", "outline-first", tok(c)))
                        ev.append(("outline",))
                    elif fp is not None and any(k in c for k in READ_DENY):
                        ev.append(("block", fp, last.get(fp, 0), tok(c)))
                    else:
                        ev.append(("inject", "other hook text", tok(c)))
                    continue
                n = SHRINK.search(c[-1500:]) if "Big Dad shrink" in c[-1500:] else None
                if n:
                    before, after = (int(g.replace(",", "")) for g in n.groups())
                    ev.append(("shrink", max(0, before - after)))
                    ev.append(("inject", "output shrink", tok(n.group(0))))
                if fp is not None and c:
                    last[fp] = tok(c)
    return ev


def tally(ev, cost, saved, fired):
    """Charge each event for the calls that re-sent it."""
    calls_after = [0] * (len(ev) + 1)        # calls after event i (whole log)
    to_compact = [0] * (len(ev) + 1)         # calls after event i, before the next compact
    for i in range(len(ev) - 1, -1, -1):
        k = ev[i][0]
        calls_after[i] = calls_after[i + 1] + (k == "call")
        to_compact[i] = 0 if k == "compact" else to_compact[i + 1] + (k == "call")
    listing, listing_seen = 0, False
    for i, x in enumerate(ev):
        k = x[0]
        if k == "listing":
            # the line rides in every later call; a newer listing row replaces the charge from here on
            if listing:
                cost["skill-list line"] -= listing * calls_after[i]
            listing = x[1]
            cost["skill-list line"] += listing * calls_after[i]
            fired["skill-list line"] += not listing_seen      # fired = logs that carried the line
            listing_seen = True
        elif k == "inject":
            cost[x[1]] += x[2] * to_compact[i]
            fired[x[1]] += 1
        elif k == "block":
            fp, size, msg = x[1], x[2], x[3]
            fired["read guard"] += 1
            cost["read guard"] += msg * to_compact[i]
            left, retried = 0, False
            for y in ev[i + 1:]:
                if y[0] == "compact":
                    break
                if y[0] == "call":
                    left += 1
                if y[0] == "read" and y[1] == fp and left <= 3:
                    retried = True
                    break
            if not retried:
                saved["read guard"] += size * to_compact[i]
        elif k == "shrink":
            saved["output shrink"] += x[1] * to_compact[i]


def logs(root, days):
    cut = time.time() - days * 86400
    for f in glob.glob(os.path.join(glob.escape(root), "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) >= cut:
                yield f
        except OSError:
            continue


def installed(claude_dir):
    """(version, ISO time it landed) of the installed Big Dad plugin, or ("", "")."""
    try:
        with open(os.path.join(claude_dir, "plugins", "installed_plugins.json"), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return "", ""
    rows = (d.get("plugins") if isinstance(d, dict) else None) or {}
    for k, v in rows.items() if isinstance(rows, dict) else ():
        if k.split("@")[0] == "bigdad" and isinstance(v, list) and v and isinstance(v[0], dict):
            return str(v[0].get("version") or ""), str(v[0].get("lastUpdated") or v[0].get("installedAt") or "")
    return "", ""


# 2.0.3: Big Dad's own build and test chats are not everyday use. A log is "building/testing Big Dad" when it ran in
# a Big Dad folder (repo, worktree, plugin folder), is a Big Dad probe or bench run (bd-headless-*, bdbench), or made
# TOUCHES+ tool calls on Big Dad's files or commands. A helper takes its parent chat's label.
DEV = re.compile(r"bigdad|big dad|bd[-_]headless|bdbench", re.I)
TOUCHES = 3


def is_dev(rel, ev):
    if DEV.search(rel.split(os.sep)[0]) or any(x[0] == "cwd" and DEV.search(x[1]) for x in ev):
        return True
    return sum(1 for x in ev if x[0] == "touch") >= TOUCHES


def bucket():
    return {"logs": 0, "calls": 0, "cost": defaultdict(int), "saved": defaultdict(int), "fired": defaultdict(int)}


def measure(root, days, since=""):
    b = {"all": bucket(), "dev": bucket(), "use": bucket()}
    rows = []
    for f in logs(root, days):
        ev = scan(f, since)
        n = sum(1 for x in ev if x[0] == "call")
        if n:
            rows.append((f, ev, n, is_dev(os.path.relpath(f, root), ev)))
    dev_sessions = {os.path.splitext(f)[0] for f, _, _, d in rows if d}
    for f, ev, n, d in rows:
        parent = os.path.dirname(os.path.dirname(f)) if os.path.basename(os.path.dirname(f)) == "subagents" else ""
        side = "dev" if d or parent in dev_sessions else "use"
        for k in ("all", side):
            b[k]["logs"] += 1
            b[k]["calls"] += n
            tally(ev, b[k]["cost"], b[k]["saved"], b[k]["fired"])
    flat = lambda x: {"logs": x["logs"], "calls": x["calls"], "cost": dict(x["cost"]), "saved": dict(x["saved"]),
                      "fired": dict(x["fired"])}
    out = dict(flat(b["all"]), days=days, since=since)
    out["split"] = {"building_testing_bigdad": flat(b["dev"]), "everyday": flat(b["use"])}
    return out


def fmt(n):
    return f"{n:,}" if n else "0"


def cmd_self(root, days=14.0, as_json=False):
    r = measure(root, days)
    ver, at = installed(os.path.dirname(root))
    now = measure(root, days, at) if at else None
    if as_json:
        print(json.dumps({"window": r, "this_version": dict(now or {}, version=ver)}, indent=1))
        return r
    cost, saved, fired = r["cost"], r["saved"], r["fired"]
    parts = sorted(set(cost) | set(saved), key=lambda p: -(cost.get(p, 0) - saved.get(p, 0)))
    tc, ts = sum(cost.values()), sum(saved.values())
    print(f"🥛 == SELF: what Big Dad costs vs what it saved, last {days:g} days "
          f"({r['logs']:,} logs incl. helpers, {r['calls']:,} API calls; ≈ = 4 chars per token)")
    if not parts:
        print("  no Big Dad text found in these logs: nothing sent, nothing saved")
        return r
    w = max(len(p) for p in parts)
    print(f"  {'part'.ljust(w)}  {'fired':>6}  {'cost ≈':>11}  {'saved ≈':>11}  {'net':>12}")
    for p in parts:
        c, s = cost.get(p, 0), saved.get(p, 0)
        print(f"  {p.ljust(w)}  {fired.get(p, 0):>6,}  {fmt(c):>11}  {fmt(s):>11}  {s - c:>+12,}")
    print(f"  {'total'.ljust(w)}  {'':>6}  {fmt(tc):>11}  {fmt(ts):>11}  {ts - tc:>+12,}")
    for key, label in (("building_testing_bigdad", "building/testing Big Dad"), ("everyday", "everyday work")):
        x = r["split"][key]
        xc, xs = sum(x["cost"].values()), sum(x["saved"].values())
        share = f"{100 * xc / tc:.0f}% of the cost" if tc else "no cost"
        print(f"  {label}: {x['logs']:,} logs, {x['calls']:,} calls: cost ≈ {fmt(xc)} ({share}), saved ≈ {fmt(xs)},"
              f" net {xs - xc:+,}")
    print("  building/testing = a chat in a Big Dad folder, a Big Dad probe or bench run, or 3+ tool calls on Big Dad"
          " files; its helpers count with it. Judge Big Dad by the everyday line.")
    if now is not None:
        nc, ns = sum(now["cost"].values()), sum(now["saved"].values())
        print(f"  this version only ({ver}, since {at[:16].replace('T', ' ')} UTC; {now['calls']:,} calls): cost ≈ "
              f"{fmt(nc)}, saved ≈ {fmt(ns)}, net {ns - nc:+,}. The window above mixes older versions; their notes"
              f" may be gone already.")
    print("  cost = Big Dad's text × the calls that re-sent it; saved = what a deny or a shrink kept out of later calls."
          " Not counted: compacts (Claude Code's own work), what bigdad.py prints when you run it, outline-first and the"
          " big-file note (fired, effect not measured).")
    losers = [p for p in parts if cost.get(p, 0) > saved.get(p, 0) and cost.get(p, 0) >= 1000]
    props = [(p, SWITCH.get(p)) for p in losers]
    if props:
        print("  parts that cost more than they saved (proposals only, nothing changed):")
        for p, how in props:
            net = saved.get(p, 0) - cost.get(p, 0)
            if how:
                print(f"    {p}: {net:+,}. Switch off with `{how}`, only on a yes; it stops what this part does too.")
            else:
                print(f"    {p}: {net:+,}. No switch: the fix is a shorter text in Big Dad (a code change, with a yes).")
    print("  Your choice wins: a part you keep on purpose is `keep <part> \"why\"`, a no is `keep <part> --declined`.")
    return r
