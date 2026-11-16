"""`bigdad.py self [DAYS]`: what Big Dad itself costs vs what it saved, per part, from Claude Code's own logs.

Cost = every token of Big Dad text that went into the model's context (its skill-list line, hook notes, deny reasons,
the skill page when invoked) times the calls that re-sent it: until the next compact for chat text, every later call
for the skill-list line. Saved = what a cut kept out of later calls: a read-guard deny (the blocked copy times the
calls left before the next compact; not credited when the model read the file again within 3 calls) and output
shrink (tokens cut times calls left, less what a later call on its saved copy brought back in). Only the notes and
calls inside the window asked for count. Text is ≈4 characters per token. Read-only; proposes, never switches anything.
"""
import glob, json, os, re, time
from collections import defaultdict

BD = re.compile(r"big ?dad|bigdad|\U0001f95b", re.I)
SHRINK = re.compile(r"Big Dad shrink: showed [\d,]+ of [\d,]+ lines \(~([\d,]+) → ~([\d,]+) tokens")
GUARD = re.compile(r"(?:\U0001f95b )?Big Dad (?:\W+ )?(?:guard: this session|headless: this run)")
NOTE_START = re.compile(r"(?=\U0001f95b Big Dad)")       # one hook's text can hold several notes, each starts so
FULL = re.compile(r"Full output: (.+?) \(Grep it")      # the shrink note's saved copy
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
    """Other text (a file, a command's output): the rough 4 chars per token; it undercounts symbol-heavy text."""
    return len(s) // 4 if s else 0


# 3.0.1: Big Dad's own texts are sized at the API's own rate, not 4 chars per token. Source: the profile of real
# sessions (message.usage in the transcripts); for skill-page fires with nothing else entering between two calls the
# context total grew by 1,760 tokens for the 4,394-byte page (2.50 bytes per token, 4 logs), 1,814 for 4,533 (2.50),
# 1,533 for 3,693 (2.41), 3,606 for 9,673 (2.68), 4,590 for 12,373 (2.70). 4 chars per token undercut it by ~38%.
OWN_BYTES_PER_TOKEN = 2.5


def own(s):
    """Tokens of Big Dad's own text (its notes, page, denies, listing line) at OWN_BYTES_PER_TOKEN, rounded up."""
    return -(-len(s.encode("utf-8", "replace")) * 2 // 5) if s else 0


SMALL = 200            # chars: a text this short may ride in a measured figure (the Skill tool's launch line)
MEASURED = "measured (API counts in the log)"
OWN_EST = "estimated (chars, 2.5 per token)"     # Big Dad's own texts, at the rate above (counted on UTF-8 bytes)
SAVED_EST = "estimated (chars, 4 per token)"     # what a deny or a shrink kept out: the hooks' own 4 chars per token
SAVED_HEAD = "saved (estimated, Big Dad's own count)"


def _out(u):
    v = u.get("output_tokens")
    return v if type(v) is int and 0 <= v <= 10 ** 12 else 0


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
    # 3.0.1 (outside test of 2.0.8): the guard's notes on UserPromptSubmit were all filed as redirect notes
    if GUARD.match(text):
        return "context guard"
    if event == "UserPromptSubmit":
        return "agent guard" if "guard: agent " in text[:40] else "redirect note"
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


def scan(path, since="", seen=None):
    """Ordered events of one log: call, compact, listing(tokens), inject(part, tokens[, copy]), read(fp),
    block(fp, blocked_tokens, deny_tokens[, copy]), shrink(cut, saved copy's name), readback(name, tokens), outline.
    3.0.1: seen = {"msg": set(), "row": set()} shared by the logs of one measure: a continued chat's new log repeats
    rows of the old one (same uuid, same message id); their calls count once and their notes are marked copies (they
    still ride in the new log's calls, but did not fire again)."""
    ev, seen_res, reads, last, cwds, back, names, rows = [], set(), {}, {}, set(), {}, set(), set()
    seen = seen if seen is not None else {"msg": set(), "row": set()}
    seen_msg = seen["msg"]
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return ev
    pend = None                                       # where a copied row's events start
    # 3.0.1: the skill page's own size from the API's counts. last = the call before: its id, context total, output;
    # page = a fire waiting for the call after it: (event index, the call before's id); extra = something else
    # entered since the call before (then the growth is not the page's alone and the estimate stays)
    last_call, page, extra = [None, 0, 0], None, False
    with fh:
        for ln in fh:
            pend = _copies(ev, pend)
            try:
                e = json.loads(ln)
            except (ValueError, RecursionError):
                continue
            if not isinstance(e, dict):
                continue
            cwd = e.get("cwd")
            if isinstance(cwd, str) and cwd not in cwds:   # where the chat ran, whatever the window (its label)
                cwds.add(cwd)
                ev.append(("cwd", cwd))
            if since and _s(e.get("timestamp")) < since:   # before the window or this version: not counted
                continue
            uid = _id(e.get("uuid"))
            copy = uid in seen["row"] if uid else False
            if uid:
                rows.add(uid)
            pend = len(ev) if copy else None
            t = e.get("type")
            if t == "api-request-blob":                      # a copy of rows already logged
                continue
            if t == "system" and e.get("subtype") == "compact_boundary":
                ev.append(("compact",))
                extra = True
                continue
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            if t == "assistant":
                mid = _id(m.get("id"))
                u = _d(m.get("usage"))
                if mid and mid == last_call[0]:              # one call's rows: its output is the largest count
                    last_call[2] = max(last_call[2], _out(u))
                elif mid and _sent(u):
                    if page is not None and page[1] == last_call[0] and last_call[0] and not extra:
                        x = ev[page[0]]
                        grew = _sent(u) - last_call[1] - last_call[2]
                        # a growth far from the page's size means the log missed something: keep the estimate
                        if x[2] // 2 <= grew <= x[2] * 2:
                            ev[page[0]] = x[:2] + (grew,) + x[3:]
                            ev.append(("api", page[0]))
                    last_call, page, extra = [mid, _sent(u), _out(u)], None, False
                # thirty-ninth review: a call counts (and marks its id) only when its input counts are above 0, the
                # rule of walk, modes and the receipt; a damaged first block no longer stands for the call
                if mid and mid not in seen_msg and _sent(m.get("usage")):
                    seen_msg.add(mid)
                    ev.append(("call",))
                for b in _l(m.get("content")):
                    if isinstance(b, dict) and b.get("type") == "tool_use" and edits_source(b):
                        ev.append(("touch",))
                    if isinstance(b, dict) and b.get("type") == "tool_use" and names and _id(b.get("id")):
                        inp = json.dumps(b.get("input"), ensure_ascii=False)
                        hit = next((n for n in names if n in inp), None)
                        if hit:                     # 3.0.1: a call on a shrink's saved copy brings the cut back in
                            back[b["id"]] = hit
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
                extra = extra or at != "command_permissions"
                if at == "invoked_skills":       # Claude Code re-attached invoked skills' pages (after a compact)
                    for sk in _l(a.get("skills")):
                        if isinstance(sk, dict) and _s(sk.get("name")).split(":")[0] == "bigdad" and sk.get("content"):
                            ev.append(("inject", "skill page (/bigdad)", own(_s(sk["content"])), "reattach"))
                elif at == "skill_listing":
                    line = listing_line(a.get("content"))
                    if line:
                        ev.append(("listing", own(line)))
                elif at.startswith("hook_") and at != "hook_system_message":   # systemMessage: shown, not sent
                    c = a.get("content")
                    c = c if isinstance(c, str) else " ".join(map(str, c)) if isinstance(c, list) else \
                        json.dumps(c, ensure_ascii=False) if c else ""
                    # 3.0.1: each note of a shared hook text is its own fire; text before the first note rides with it
                    pieces = NOTE_START.split(c) if c and BD.search(c) else []
                    if len(pieces) > 1 and not pieces[0].startswith("\U0001f95b Big Dad"):
                        head = pieces.pop(0)
                        pieces[0] = head + pieces[0]
                    for piece in pieces:
                        ev.append(("inject", part_of(_s(a.get("hookEvent")), piece), own(piece)))
                continue
            if t != "user":
                continue
            body = m.get("content")
            if not isinstance(body, list):
                # the /bigdad prompt or the Skill tool's launch line may ride in the measured figure: a few tokens,
                # never in Big Dad's favour; anything longer leaves the estimate
                extra = extra or len(_s(body)) > SMALL
                continue
            for b in body:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    s = _s(b.get("text"))
                    if "Base directory for this skill" in s[:400] and "bigdad" in s[:400].lower():
                        extra = extra or page is not None
                        ev.append(("inject", "skill page (/bigdad)", own(s)))
                        page = (len(ev) - 1, last_call[0])
                    else:
                        extra = extra or len(s) > SMALL
                    continue
                extra = extra or b.get("type") != "tool_result" or len(text_of(b.get("content"))) > SMALL
                if b.get("type") != "tool_result":
                    continue
                tid = _id(b.get("tool_use_id"))
                if tid and tid in seen_res:
                    continue
                if tid:
                    seen_res.add(tid)
                c = text_of(b.get("content"))
                if tid in back and not b.get("is_error"):
                    ev.append(("readback", back.pop(tid), own(c)))
                fp = reads.get(tid) if tid else None   # thirty-ninth review: an id-less result matches no Read
                if b.get("is_error") and "Big Dad" in c[:120] and not c.startswith("Exit code"):   # Big Dad's own deny,
                    # not a failed command whose output happens to mention Big Dad
                    if OUTLINE in c:
                        ev.append(("inject", "outline-first", own(c)))
                        ev.append(("outline",))
                    elif fp is not None and any(k in c for k in READ_DENY):
                        ev.append(("block", fp, last.get(fp, 0), own(c)))
                    else:
                        ev.append(("inject", "other hook text", own(c)))
                    continue
                n = SHRINK.search(c[-1500:]) if "Big Dad shrink" in c[-1500:] else None
                if n:
                    before, after = (int(g.replace(",", "")) for g in n.groups())
                    w = FULL.search(c[-1500:], n.end())
                    name = re.split(r"[\\/]", w.group(1))[-1] if w else ""
                    if name:
                        names.add(name)
                    ev.append(("shrink", max(0, before - after), name))
                    ev.append(("inject", "output shrink", own(n.group(0))))
                if fp is not None and c:
                    last[fp] = tok(c)
    _copies(ev, pend)
    seen["row"] |= rows
    return ev


def _copies(ev, start):
    """Mark the notes and denies from ev[start:] as copies of an earlier log's rows (they did not fire again)."""
    if start is not None:
        for j in range(start, len(ev)):
            if ev[j][0] in ("inject", "block", "shrink") and ev[j][-1] is not True:
                ev[j] = ev[j] + (True,)
    return None


def tally(ev, cost, saved, fired, back=None, rides=None, measured=None):
    """Charge each event for the calls that re-sent it. A copy (a row repeated from an earlier log) is charged for
    this log's calls but does not fire again. back counts shrinks and the shrinks read back (3.0.1). rides: the skill
    page's re-attachments after a compact; measured: the part of each cost sized by the API's own counts."""
    back = back if back is not None else {}
    rides = rides if rides is not None else defaultdict(int)
    measured = measured if measured is not None else defaultdict(int)
    api = {x[1] for x in ev if x[0] == "api"}
    page = ""                                  # "riding" from a fire, "compact" after a compact (a re-attach may ride)
    calls_after = [0] * (len(ev) + 1)        # calls after event i (whole log)
    to_compact = [0] * (len(ev) + 1)         # calls after event i, before the next compact
    for i in range(len(ev) - 1, -1, -1):
        k = ev[i][0]
        calls_after[i] = calls_after[i + 1] + (k == "call")
        to_compact[i] = 0 if k == "compact" else to_compact[i + 1] + (k == "call")
    listing, listing_seen = 0, False
    for i, x in enumerate(ev):
        k = x[0]
        if k == "compact":
            page = "compact"
        elif k == "inject" and len(x) > 3 and x[3] == "reattach":
            # Claude Code re-attached the page after a compact: it rides the calls to the next compact. Only a row
            # after a compact counts (one beside a fire would charge the same calls twice); it is not a new fire
            if page == "compact":
                page = "riding"
                cost[x[1]] += x[2] * to_compact[i]
                rides["cost"] += x[2] * to_compact[i]
                rides["reattached"] += x[-1] is not True
        elif k == "inject":
            if x[1] == "skill page (/bigdad)":
                page = "riding"
            cost[x[1]] += x[2] * to_compact[i]
            if i in api:
                measured[x[1]] += x[2] * to_compact[i]
            fired[x[1]] += len(x) < 4
        elif k == "listing":
            # the line rides in every later call; a newer listing row replaces the charge from here on
            if listing:
                cost["skill-list line"] -= listing * calls_after[i]
            listing = x[1]
            cost["skill-list line"] += listing * calls_after[i]
            fired["skill-list line"] += not listing_seen      # fired = logs that carried the line
            listing_seen = True
        elif k == "block":
            fp, size, msg = x[1], x[2], x[3]
            fired["read guard"] += len(x) < 5
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
            # 3.0.1: what a later call on its saved copy brought back in (that call's result x the calls that
            # re-sent it) is not saved; never below 0 for one shrink
            got = x[1] * to_compact[i]
            again = [j for j in range(i + 1, len(ev)) if ev[j][0] == "readback" and x[2] and ev[j][1] == x[2]]
            got -= sum(ev[j][2] * to_compact[j] for j in again)
            saved["output shrink"] += max(0, got)
            if len(x) < 4:                   # a copy's shrink was counted in its first log
                back["shrinks"] = back.get("shrinks", 0) + 1
                back["read_back"] = back.get("read_back", 0) + bool(again)


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
# Big Dad's source tree (repo, worktree, plugin folder), is a Big Dad probe or bench run (bd-headless-*, bdbench), or
# made TOUCHES+ edits of Big Dad's source files. A helper takes its parent chat's label.
# 3.0.1 (outside test of 2.0.8: 1,241 of 1,267 calls filed as building/testing on a machine not building Big Dad):
# running Big Dad's commands, its skill page, reading its output files or a folder that only has "bigdad" in its name
# is everyday work. A source tree is a folder holding hooks/bd_common.py and skills/bigdad/scripts/bd_self.py, checked
# on disk now (a chat in a removed worktree counts by the repo above it; a removed tree counts by its edits only).
DEV = re.compile(r"bigdad|big dad|bd[-_]headless|bdbench", re.I)
PROBE = re.compile(r"bd[-_]headless|bdbench", re.I)
TOUCHES = 3
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_trees = {}


def in_source(path):
    """True when path is in (or is) a Big Dad source tree, judged by the folders above it that exist."""
    if not path or not DEV.search(path):
        return False
    if path not in _trees:
        # a cwd recorded on Windows has backslashes, which Linux's path functions do not split on
        p, hit = os.path.normpath(path.replace("\\", "/") if os.sep == "/" else path), False
        while True:
            if os.path.isfile(os.path.join(p, "hooks", "bd_common.py")) and \
                    os.path.isfile(os.path.join(p, "skills", "bigdad", "scripts", "bd_self.py")):
                hit = True
                break
            up = os.path.dirname(p)
            if up == p:
                break
            p = up
        _trees[path] = hit
    return _trees[path]


def edits_source(b):
    """A tool call that edits or writes a file of Big Dad's source tree."""
    if b.get("name") not in EDITS:
        return False
    inp = _d(b.get("input"))
    return in_source(_s(inp.get("file_path")) or _s(inp.get("notebook_path")))


def is_dev(rel, ev):
    if PROBE.search(rel.split(os.sep)[0]) or any(x[0] == "cwd" and (PROBE.search(x[1]) or in_source(x[1])) for x in ev):
        return True
    return sum(1 for x in ev if x[0] == "touch") >= TOUCHES


def bucket():
    return {"logs": 0, "calls": 0, "cost": defaultdict(int), "saved": defaultdict(int), "fired": defaultdict(int),
            "read_back": {"shrinks": 0, "read_back": 0}, "rides": defaultdict(int), "measured": defaultdict(int)}


def basis(measured, total):
    """The label every cost figure carries: measured only when all of it came from the API's own counts."""
    if total and measured >= total:
        return MEASURED
    return f"{OWN_EST[:-1]}; {measured:,} of it {MEASURED})" if measured else OWN_EST


def measure(root, days, since=""):
    b = {"all": bucket(), "dev": bucket(), "use": bucket()}
    rows, seen = [], {"msg": set(), "row": set()}

    def age(f):
        try:
            return os.path.getmtime(f), f
        except OSError:
            return 0, f
    for f in sorted(logs(root, days), key=age):      # oldest first: a continued chat's old log before its new one
        ev = scan(f, since, seen)
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
            tally(ev, b[k]["cost"], b[k]["saved"], b[k]["fired"], b[k]["read_back"], b[k]["rides"], b[k]["measured"])

    def flat(x):
        cost, meas = dict(x["cost"]), {p: v for p, v in x["measured"].items() if v}
        return {"logs": x["logs"], "calls": x["calls"], "cost": cost, "saved": dict(x["saved"]),
                "fired": dict(x["fired"]), "read_back": dict(x["read_back"]),
                "skill_page_rides": {"reattached": x["rides"]["reattached"], "cost": x["rides"]["cost"]},
                "measured_cost": meas,
                "basis": {"cost": {p: basis(meas.get(p, 0), c) for p, c in cost.items()},
                          "saved": {p: SAVED_EST for p in x["saved"]}}}
    out = dict(flat(b["all"]), days=days, since=since)
    out["split"] = {"building_testing_bigdad": flat(b["dev"]), "everyday": flat(b["use"])}
    return out


def fmt(n):
    return f"{n:,}" if n else "0"


def cmd_self(root, days=14.0, as_json=False):
    # 3.0.1 (outside test of 2.0.8): the window asked for is the window counted. A log touched in the window was
    # counted whole, and "this version only" began at the install, before the window: both now start at the window
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
    r = measure(root, days, start)
    ver, at = installed(os.path.dirname(root))
    newer = bool(at) and at > start                   # installed inside the window: the version line is a part of it
    now = measure(root, days, at) if newer else None
    if as_json:
        print(json.dumps({"window": r, "this_version": dict(now or (r if at else {}), version=ver, installed=at)},
                         indent=1))
        return r
    cost, saved, fired = r["cost"], r["saved"], r["fired"]
    parts = sorted(set(cost) | set(saved), key=lambda p: -(cost.get(p, 0) - saved.get(p, 0)))
    tc, ts = sum(cost.values()), sum(saved.values())
    print(f"🥛 == SELF: what Big Dad costs vs what it saved, last {days:g} days, from {start[:16].replace('T', ' ')} UTC "
          f"({r['logs']:,} logs incl. helpers, {r['calls']:,} API calls). Notes and calls before that are left out.")
    if not parts:
        print("  no Big Dad text found in these logs: nothing sent, nothing saved")
        return r
    print(f"  cost is {MEASURED} where a log shows the context grow by the skill page alone between two calls; else"
          f" {OWN_EST}: Big Dad's own texts, at the rate measured on real sessions, counted on bytes. Saved is"
          f" {SAVED_EST}: Big Dad's own count of what a deny or a shrink kept out, a rough rule that undercounts"
          " symbol-heavy text; no log holds what the full text would have cost, so saved is never measured.")
    w, sw = max(len(p) for p in parts), len(SAVED_HEAD)
    print(f"  {'part'.ljust(w)}  {'fired':>6}  {'cost':>11}  {SAVED_HEAD:>{sw}}  {'net':>12}  cost is")
    for p in parts:
        c, s = cost.get(p, 0), saved.get(p, 0)
        print(f"  {p.ljust(w)}  {fired.get(p, 0):>6,}  {fmt(c):>11}  {fmt(s):>{sw}}  {s - c:>+12,}"
              f"  {r['basis']['cost'].get(p, OWN_EST) if c else '-'}")
    print(f"  total: cost {fmt(tc)} [{basis(sum(r['measured_cost'].values()), tc)}], {SAVED_HEAD} {fmt(ts)},"
          f" net {ts - tc:+,}")
    rides = r["skill_page_rides"]
    if rides["reattached"]:
        n = rides["reattached"]
        print(f"  skill page: Claude Code re-attached the skill page after {n:,} compact{'s' * (n != 1)} (the log's"
              f" invoked_skills rows), so it rides on until the session ends; {fmt(rides['cost'])} of its cost is"
              f" the calls after those compacts [{OWN_EST}].")
    rb = r["read_back"]
    if rb.get("shrinks"):
        print(f"  output shrink: {rb['read_back']:,} of {rb['shrinks']:,} shrinks were read back from their saved copy;"
              " what those reads brought back is taken off saved (the reads are in the log; their size is"
              f" {OWN_EST}, so saved errs low).")
    for key, label in (("building_testing_bigdad", "building/testing Big Dad"), ("everyday", "everyday work")):
        x = r["split"][key]
        xc, xs = sum(x["cost"].values()), sum(x["saved"].values())
        share = f"{100 * xc / tc:.0f}% of the cost" if tc else "no cost"
        print(f"  {label}: {x['logs']:,} logs, {x['calls']:,} calls: cost {fmt(xc)}"
              f" [{basis(sum(x['measured_cost'].values()), xc)}] ({share}), {SAVED_HEAD} {fmt(xs)}, net {xs - xc:+,}")
    print("  building/testing = a chat in Big Dad's source tree, a Big Dad probe or bench run, or 3+ edits of Big Dad's"
          " source files; its helpers count with it. Using Big Dad (its commands, skill page and output files) is"
          " everyday work. Judge Big Dad by the everyday line.")
    if at and not newer:
        print(f"  this version ({ver}) was installed before the window ({at[:16].replace('T', ' ')} UTC): every figure"
              " above is this version's.")
    if now is not None:
        nc, ns = sum(now["cost"].values()), sum(now["saved"].values())
        print(f"  this version only ({ver}, since {at[:16].replace('T', ' ')} UTC; {now['calls']:,} calls): cost "
              f"{fmt(nc)} [{basis(sum(now['measured_cost'].values()), nc)}], {SAVED_HEAD} {fmt(ns)},"
              f" net {ns - nc:+,}. The window above mixes older versions; their notes may be gone already.")
    print("  cost = Big Dad's text × the calls that re-sent it; saved = what a deny or a shrink kept out of later calls."
          " Both run until the chat's next compact (the skill page goes on where the log shows Claude Code"
          " re-attached it), so a part's figures keep growing while a long chat goes on, with no new fire. A"
          " continued chat's repeated rows count once."
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
