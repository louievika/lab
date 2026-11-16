#!/usr/bin/env python3
"""Big Dad slim compact (Claude Code PreCompact hook, manual and auto).

Claude Code uses a PreCompact hook's stdout as instructions for the compaction summary
(docs: hooks, PreCompact; cap 10,000 chars per hook; fires for /compact and for auto-compact).
Big Dad reads the session since the last compact and tells the summary what is dead weight
(file contents that were edited afterwards, file text already written to disk, long scripts,
big raw outputs, finished agents) and what must survive (the user's asks and rules, open work,
running agents, measured numbers, commits). It never deletes anything itself.

The user's own `/compact <text>` arrives as `custom_instructions`. How Claude Code combines
that with hook stdout is not documented, so Big Dad repeats the user's text first, marked as
the top priority: it survives either way.

  BIGDAD_SLIM=off     turn it off
  python3 compact_slim.py --show TRANSCRIPT   print what it would send, with token estimates
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

BIG = 20_000         # characters (~5k tokens): a non-read tool output this big is "raw output" (RULES-SPEC R4)
CAP = 9_500          # stay under Claude Code's 10,000-char cut
TAIL = 8 << 20       # bytes: read only the transcript's tail (a 50 MB log stays under 200 ms)
LIST_MAX = 20        # named stale reads + big outputs, one short line each


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    try:
        return json.dumps(content or "")
    except (TypeError, ValueError, RecursionError):   # twentieth review: too deep for json.dumps
        return ""


def _lines(path):
    """The transcript's lines, from its last TAIL bytes only (the first line is skipped when the cut splits it)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size > TAIL:
            f.seek(size - TAIL - 1)
            if f.read(1) != b"\n":
                f.readline()
        else:
            f.seek(0)
        for raw in f:
            yield raw.decode("utf-8", "replace")


def scan(path, keep=None):
    """Walk the transcript since the last compact boundary (within its tail). keep: a list that gets the rows."""
    rows, done = [], set()
    for line in _lines(path):
        try:
            d = json.loads(line)                   # 3.0.0 fourteenth review: each line parsed once
        except (ValueError, RecursionError):
            continue
        if not isinstance(d, dict):
            continue
        if '"compact_boundary"' in line and '"subtype"' in line and d.get("subtype") == "compact_boundary":
            rows, done = [], set()
            continue
        if d.get("type") == "api-request-blob":    # a copy of rows already seen
            continue
        if "<task-id>" in line:
            for tid, status in re.findall(r"<task-id>(\w+)</task-id>.*?<status>(\w+)</status>", line, re.S):
                if status in ("completed", "killed", "failed"):
                    done.add(tid)
        rows.append(d)
    if keep is not None:
        keep[:] = rows
    calls, results, order, sizes = {}, {}, [], {}
    for i, d in enumerate(rows):
        try:
            new, res = _row_calls(i, d, calls)
        except C.ODD_ROW:
            continue                               # 3.0.0 fifteenth review: a row that cannot be read: skipped whole
        for cid, v in new:                         # only now is anything counted
            calls[cid] = v
            order.append(cid)
        for tid, text, size in res:
            results[tid], sizes[tid] = text, size
    return calls, results, order, done, sizes


def _row_calls(i, d, calls):
    """Row i of scan, read whole before anything is counted: ([(tool_use id, call)], [(tool_use_id, text, size)]).
    Raises on a value of the wrong kind (a list "id" or "tool_use_id", a "text" that is not a string...)."""
    m = d.get("message")
    content = m.get("content") if isinstance(m, dict) else None
    new, res, ids = [], [], set()
    if not isinstance(content, list):
        return new, res
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_use":
            cid = c.get("id")
            if cid and (cid in calls or cid in ids):                     # the same call logged twice: count it once
                continue
            ids.add(cid)      # seventeenth review: a list or dict id raises TypeError here or just above: row skipped
            inp = c.get("input")
            inp = inp if isinstance(inp, dict) else {}
            new.append((cid, (i, C.dstr(c.get("name")), inp, C.dstr(d.get("cwd")))))
        elif c.get("type") == "tool_result":
            tid = c.get("tool_use_id")
            if isinstance(tid, (list, dict)):
                raise TypeError("a tool_use_id that cannot be a key")
            res.append((tid, text_of(c.get("content")), C.est(c.get("content"))[0]))   # images as images, not base64
    return new, res


def _norm(p, cwd):
    try:
        import read_guard as R
        return R.norm_path(p, cwd)
    except Exception:
        return None


def _dump_paths(name, inp, cwd):
    if name not in ("Bash", "PowerShell"):
        return []
    try:
        import read_guard as R
        return [p for p, _ in (R.parse_dump(C.dstr(inp.get("command")), cwd, name) or [])]
    except Exception:
        return []


def bash_writes(cmd, cwd):
    """Paths a shell command writes: > / >> targets, tee, sed -i / perl -i, cp/mv/touch/truncate targets."""
    out = set()
    for m in re.finditer(r"(?<![0-9&])>{1,2}\s*([^\s;&|]+)", cmd):
        if m.group(1) not in ("/dev/null", "&1", "&2"):
            out.add(_norm(m.group(1), cwd))
    for seg in re.split(r"&&|\|\||;|\n|\|", cmd):
        t = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", seg.strip())
        if not t:
            continue
        v = t[0].lower()
        args = [x for x in t[1:] if not x.startswith("-")]
        if v == "tee" or (v in ("sed", "perl") and any(x.startswith("-i") for x in t[1:])):
            out.update(_norm(x, cwd) for x in (args if v == "tee" else args[1:]))
        elif v in ("cp", "mv") and len(args) > 1:
            out.add(_norm(args[-1], cwd))
        elif v in ("touch", "truncate", "set-content", "add-content", "out-file"):
            out.update(_norm(x, cwd) for x in args)
    out.discard(None)
    return out


def _label(name, inp):
    if name in ("Bash", "PowerShell"):
        cmd = " ".join(C.dstr(inp.get("command")).split())
        return f"{name} `{cmd[:50]}{'…' if len(cmd) > 50 else ''}`"
    detail = C.dstr(inp.get("description")) or os.path.basename(C.dstr(inp.get("file_path"))) or C.dstr(inp.get("pattern"))
    return f"{name} ({detail[:40]})" if detail else name


def plan(path, keep=None):
    """stale: {path: (name, chars)} reads/dumps of files written later; raw: [(label, chars)] big non-read outputs."""
    calls, results, order, done, sizes = scan(path, keep)
    last_write = {}
    for tid in order:
        i, name, inp, cwd = calls[tid]
        fp = C.dstr(inp.get("file_path")) or C.dstr(inp.get("notebook_path"))
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and fp:
            k = _norm(fp, cwd)
            if k:
                last_write[k] = i
        elif name in ("Bash", "PowerShell"):
            for k in bash_writes(C.dstr(inp.get("command")), cwd):
                last_write[k] = i
    stale, raw, stopped, running, payload = {}, [], [], {}, 0
    for tid in order:
        i, name, inp, cwd = calls[tid]
        out = results.get(tid, "")
        n = sizes.get(tid, len(out))
        fp = C.dstr(inp.get("file_path"))
        reads = ([_norm(fp, cwd)] if name == "Read" and fp else []) + _dump_paths(name, inp, cwd)
        for k in [k for k in reads if k]:
            if last_write.get(k, -1) > i:
                old = stale.get(k, ("", 0))
                stale[k] = (os.path.basename(k), old[1] + n // max(1, len(reads)))
        if name == "Agent":
            m = re.search(r"agentId: (\w+)", out)
            if m:
                running[m.group(1)] = C.dstr(inp.get("description")) or "agent"
        elif name == "TaskStop":
            tid2 = C.dstr(inp.get("task_id")) or C.dstr(inp.get("shell_id"))
            if tid2 in running:
                stopped.append(running.pop(tid2))
        if n >= BIG and name != "Read" and not reads:
            raw.append((_label(name, inp), n))
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            try:
                payload += len(json.dumps(inp))
            except (TypeError, ValueError, RecursionError):   # twentieth review: too deep for json.dumps
                pass
        elif name == "Bash" and len(C.dstr(inp.get("command"))) > 1500:
            payload += len(inp["command"])
    for k in [k for k in running if k in done]:
        stopped.append(running.pop(k))
    return stale, raw, stopped, running, payload


def named_lists(stale, raw):
    """One short line per item, biggest first, at most LIST_MAX in all: names and sizes only."""
    items = [(n, "stale", nm) for nm, n in stale.values()] + [(n, "raw", lb) for lb, n in raw]
    items.sort(key=lambda x: -x[0])
    items = items[:LIST_MAX]
    out = []
    st = [f"- {nm} (~{n // 4 / 1000:.1f}k tokens)" for n, kind, nm in items if kind == "stale"]
    rw = [f"- {lb} (~{n // 4 / 1000:.1f}k tokens)" for n, kind, lb in items if kind == "raw"]
    if st:
        out.append("STALE READS (the file was edited after this read; drop the old text, keep what changed and why):")
        out += st
    if rw:
        out.append("BIG RAW OUTPUTS (drop the raw text; keep only the conclusions and numbers they produced):")
        out += rw
    return out


def dropped_plan(sid, path=None):
    """The plan the user dropped this chat ('scrap this, start over'), left by the context guard's redirect detector,
    or None. Not cleared here (PreCompact runs before the compact, which can still fail or be cancelled): the
    transcript size is recorded, and the flag is cleared once a compact_boundary lands past it (or at SessionStart
    "compact"). A cancelled compact leaves it for the next one."""
    if not sid:
        return None
    flag = C.redirect_file(sid)
    if not os.path.exists(flag):
        return None
    with C.lock(flag):
        st = C.load_json(flag)
        if C.settle_dropped(st, path):
            d = None                    # an earlier compact already dropped it
        else:
            d = st.get("dropped")
            if not isinstance(d, dict):
                return None
            try:
                d["compact_at"] = os.path.getsize(path) if path else 0
            except OSError:
                d["compact_at"] = 0
        try:
            with open(flag, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except OSError:
            pass
    return d


def instructions(path, user_text="", sid="", info=None):
    """The PreCompact stdout. Scored (compact_score.py) unless BIGDAD_SCORED=off; any error in the scored part
    falls back to the classic text, so a compact never loses the old behaviour. info: a dict that gets the receipt."""
    rows = []
    stale, raw, stopped, running, payload = plan(path, rows)
    dropped = dropped_plan(sid, path)   # once: it records the compact point in the redirect flag
    classic =_classic(stale, raw, stopped, running, payload, user_text, dropped)
    # a lone surrogate (a broken emoji in the chat) would make the hook's UTF-16 count raise and print nothing
    classic = classic.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if C.off("BIGDAD_SCORED"):
        return classic
    try:
        import compact_score as S
        rows = S.older_asks(path, TAIL, rows) + rows   # the tail held too few of the user's asks: look further back
        return _scored(rows, stale, raw, stopped, running, payload, user_text, dropped, info)
    except Exception:          # scoring is extra: never lose the classic instructions
        return classic


def _classic(stale, raw, stopped, running, payload, user_text, dropped):
    """The 1.7 R4 instructions, unchanged: the fallback and the BIGDAD_SCORED=off text."""
    lines = []
    if user_text.strip():
        lines.append("USER'S OWN COMPACT INSTRUCTIONS (top priority, follow first): " + user_text.strip()[:3_000])
    lines.append("🥛 Big Dad slim compact. Keep the summary lean: it is re-sent on every later call.")
    if dropped:
        said = C.dstr(dropped.get("prompt"))[:160] or C.dstr(dropped.get("phrase"))
        lines.append(f"SUPERSEDED PLAN: the user dropped the earlier plan (they said: \"{said}\"). Drop that plan and "
                     "everything made for it (its drafts, screens, previews, jobs, reasoning and file contents). Keep one line "
                     "only: the new direction, and what the user disliked about the old one.")
    drop = []
    if stale:
        drop.append(f"old contents of {len(stale)} files that were edited afterwards (STALE READS below)")
    if raw:
        drop.append(f"{len(raw)} big raw tool outputs (BIG RAW OUTPUTS below)")
    if stopped:
        drop.append(f"agents that finished or were stopped ({', '.join(stopped[:8])}): one line each, no details")
    if payload:
        drop.append("the text of files written or edited and of long scripts run: it is on disk now; keep the file path and one line on what it does")
    drop.append("earlier wordings of a goal or request the user later changed (keep the newest), and side questions already answered")
    drop.append("finished steps, retries and errors that were fixed, and any file content that can be read again")
    lines.append("DROP: " + "; ".join(drop) + ".")
    try:
        lines += named_lists(stale, raw)
    except Exception:          # the lists are extra: never lose the rest
        pass
    keep = ["the END GOAL first, in one line in the user's newest words, then the changes made toward it as a short update list (path: what changed)", "every request and rule the user gave, in their words where short",
            "open tasks and the very next step", "measured numbers, commit hashes, versions",
            "the files being worked on (paths) and what state they are in"]
    if running:
        keep.append("agents still running: " + ", ".join(f"{v} (id {k})" for k, v in list(running.items())[:8]))
    lines.append("KEEP: " + "; ".join(keep) + ".")
    return "\n".join(lines)[:CAP]


def _dead(stale, raw, payload):
    rows = [(f"stale reads ({len(stale)} files)", sum(n for _, n in stale.values())),
            (f"big raw outputs ({len(raw)})", sum(n for _, n in raw)),
            ("written files + long scripts", payload)]
    return [r for r in rows if r[1]]


def _scored(rows, stale, raw, stopped, running, payload, user_text, dropped, info=None):
    """Scored instructions: the user's words and the live work quoted (MUST SURVIVE), dead weight named, a size
    budget for the rest. Built to fit CAP whole: the quotes get the room the fixed parts leave."""
    import compact_score as S
    s = S.collect(rows, writes_of=bash_writes, is_deny=C.is_read_deny)
    head = []
    if user_text.strip():
        head.append("USER'S OWN COMPACT INSTRUCTIONS (top priority, follow first): " + S.redact(user_text.strip()[:3_000]))
    head.append("🥛 Big Dad slim compact (scored). Keep the summary lean: it is re-sent on every later call.")
    if dropped:
        said = S.redact(C.dstr(dropped.get("prompt"))[:160] or C.dstr(dropped.get("phrase")))
        head.append(f"SUPERSEDED PLAN: the user dropped the earlier plan (they said: \"{said}\"). Drop that plan and "
                    "everything made for it (its drafts, screens, previews, jobs, reasoning and file contents). Keep one line "
                    "only: the new direction, and what the user disliked about the old one.")
    drop = []
    if stale:
        drop.append(f"old contents of {len(stale)} files that were edited afterwards (STALE READS below)")
    if raw:
        drop.append(f"{len(raw)} big raw tool outputs (BIG RAW OUTPUTS below)")
    if stopped:
        drop.append(f"agents that finished or were stopped ({', '.join(S.one_line(x, 60) for x in stopped[:8])}): "
                    "one line each, no details")
    if payload:
        drop.append("the text of files written or edited and of long scripts run: it is on disk now; keep the path only")
    if s.get("fixed"):
        drop.append(f"{s['fixed']} errors that were fixed later (name the fix in one line, no traceback)")
    drop.append("earlier wordings of a goal or request the user later changed (keep the newest), and side questions already answered")
    drop.append("finished steps, retries, re-reads of the same file, and anything that can be read again")
    tail = ["DROP: " + "; ".join(drop) + "."]
    try:
        tail += named_lists(stale, raw)
    except Exception:
        pass
    keep = ["the END GOAL first, in one line in the user's newest words, then the changes made toward it as a short update list (path: what changed)", "the MUST SURVIVE lines (each FILE with its state in a few words)", "every other open request of the user, "
            "in their words where short (done ones: one line)", "open tasks and the very next step",
            "measured numbers, commit hashes, versions"]
    if running:
        keep.append("agents still running: " + ", ".join(f"{S.one_line(v, 60)} (id {S.one_line(k, 40)})"
                                                         for k, v in list(running.items())[:8]))
    tail.append("KEEP: " + "; ".join(keep) + ".")
    tail.append(S.size_rule(s))
    head = [S.redact(x) for x in head]
    tail = [S.redact(S.one_line(x, 1_500)) for x in tail]
    intro = "MUST SURVIVE (Big Dad scored this chat; copy each line into the summary word for word, user's words first):"

    def build(tail):
        fixed = sum(len(x) + 1 for x in head + tail) + len(intro) + 1
        lines, kept = S.block(s, max(0, CAP - fixed - 50))
        return "\n".join(head + ([intro] + lines if lines else []) + tail), lines, kept

    out, lines, kept = build(tail)
    if len(tail) > 3 and (S.u16(out) > CAP or kept.get("rules", 0) < S.rules_wanted(s)):
        out, lines, kept = build([tail[0], tail[-2], tail[-1]])   # the named lists give way to the user's rules
    if info is not None:
        info.update(summary=s, kept=kept, kept_chars=sum(len(l) for l in lines), dead=_dead(stale, raw, payload))
    return S.clamp16(out, CAP)    # the asks come right after the head: a cut here only shortens the tail


def main(event=None, standalone=True):
    if standalone and len(sys.argv) > 2 and sys.argv[1] == "--show":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        stale, raw, _, _, payload = plan(sys.argv[2])
        info = {}
        print(instructions(sys.argv[2], info=info))
        dead = sum(n for _, n in stale.values()) + sum(n for _, n in raw) + payload
        print(f"\n[estimate] dead weight now in context: ~{dead // 4:,} tokens "
              f"({len(stale)} superseded file reads, {len(raw)} big outputs, ~{payload // 4:,} in written files and long scripts)")
        if info:
            import compact_score as S
            print("\n".join(S.receipt(info["summary"], info["kept"], info["kept_chars"], info["dead"])))
        return
    if C.off("BIGDAD_SLIM"):
        return
    event = C.read_event() if event is None else event
    if event is None:
        return
    path = C.dstr(event.get("transcript_path"))
    if not os.path.isfile(path):
        return
    if C.learning():           # learning first (2.0.6): counted; the native compact (and the user's own text) as is
        C.learn_seen("compact")
        return
    try:
        return C.emit(instructions(path, C.dstr(event.get("custom_instructions")), C.dstr(event.get("session_id"))), standalone)
    except Exception:          # never break a compact: no instructions is the safe failure
        return


if __name__ == "__main__":
    main()
