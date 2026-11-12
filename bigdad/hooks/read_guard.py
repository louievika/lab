#!/usr/bin/env python3
"""Big Dad read guard: keep it hot (Claude Code PreToolUse hook on Read, plus PostToolUse and PreCompact).

Everything in context is re-sent on every call, so reading a file that is already in context
pays for it twice: once now, and again on every later call. This hook stops a Read of the same
file and range while the first copy is still in context and the file hasn't changed.

  - A read is "in context" only once PostToolUse saw its result (post_tool.py runs `after`): a Read
    that failed, was denied or whose result never came back is never used to block the next one.
  - Changed file (new modified time or size): allowed.
  - Different offset/limit: allowed when it asks for lines not yet in context. A range that sits wholly
    inside lines an earlier Read already put in context (same file, unchanged) is denied, and the deny
    names those lines (v1.7: Claude Code 2.1.317 dropped its own "file unchanged" Read stub).
  - Partial copy (Claude Code cut the view at its token cap or its default page): the block still
    saves the big re-send, but the deny names the lines in context and the offset for the rest, so
    the agent reads the tail instead of guessing from a partial view (versus run of 2026-11-08: the
    Read view also numbers a trailing empty line, so its total is one over `wc -l`; the deny says so).
  - Same read asked for twice in a row: the second ask is allowed, so the guard can never
    lock the agent out of a file it really needs again.
  - After /compact (or auto-compact) the session forgets what it read, so the slate is wiped.
  - Images, PDFs (per pages value) and notebooks are recorded too (v1.7 R1), so an unchanged one isn't sent twice.
  - State writes merge one key under a short lock and land with an atomic rename, so parallel Reads in one
    batch never drop each other's keys; a lock held past 200 ms makes the hook give up silently (fail open).

  - Bash/PowerShell (v1.7 R2): a command that only dumps files (cat, head/tail -n, sed -n 'A,Bp', Get-Content...)
    is denied when every file is unchanged and already in context, from the same dump or a full Read. Anything
    that writes, pipes into another tool, greps or counts, or touches a changed or new file goes through.

  - Helper size floor (1.7): in a helper agent (the hook input carries agent_id; the main chat and a `--agent`
    session don't), a re-read whose earlier copy is under the floor goes through with no note. Auto floor = the
    helper's newest call size / its calls so far (bd_common.FLOOR_RETRY_ODDS has the data), so a short helper lets a
    small re-read through (the bench: a retry cost one more ~26k call to save ~2k) and a long one, with many calls
    left to re-send the copy, still has it stopped. The main chat is unchanged.

  BIGDAD_READ_GUARD=off   turn it off
  BIGDAD_BASH_GUARD=off   turn off only the Bash/PowerShell part
  BIGDAD_HELPER_FLOOR=auto|off|4k   the helper floor (also `bigdad.py helper-floor ...`); bad values mean auto
"""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C


def state_path(session):
    return os.path.join(C.state_dir(), f"bigdad-reads-{session or 'x'}.json")


def load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


LOCK_BUDGET = 0.2      # seconds: wait this long for another writer, then give up silently
LOCK_STALE = 5.0       # seconds: a lock file older than this was left by a dead process


def _lock(path):
    lock = path + ".lock"
    end = time.monotonic() + LOCK_BUDGET
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lock
        except PermissionError:        # Windows: the last holder's lock file is still being deleted
            pass
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > LOCK_STALE:
                    os.remove(lock)
                    continue
            except OSError:
                pass
        except OSError:
            return None
        if time.monotonic() >= end:
            return None
        time.sleep(0.01)


def update(path, fn):
    """Load the state fresh, let fn(seen) change it in place, and write it back with an atomic rename, all under
    the lock, so parallel hooks merge instead of the last writer dropping the others' keys. No lock: no write."""
    lock = _lock(path)
    if lock is None:
        return False
    try:
        seen = load(path)
        if fn(seen) is False:
            return True
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f)
        for i in range(5):             # Windows: a reader holding the file open makes the rename fail for a moment
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.01)
    except OSError:
        return False
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def put(path, key, value, keep_seen=False):
    """Merge one key. keep_seen: never replace an entry that is already "seen" with the same stamp."""
    def fn(seen):
        old = seen.get(key)
        if keep_seen and isinstance(old, dict) and old.get("seen") and old.get("stamp") == value.get("stamp"):
            return False
        seen[key] = value
    return update(path, fn)


def save(path, seen):
    """Whole-state write (kept for callers and tests): merges every key of seen into the file."""
    return update(path, lambda cur: cur.update(seen))


def read_key(args):
    """(state key, file path, [mtime_ns, size]) for a Read's tool_input, or None when the path is missing or odd."""
    if not isinstance(args, dict):
        return None
    fp = C.dstr(args.get("file_path"))
    try:
        st = os.stat(fp)
    except (OSError, ValueError):
        return None
    try:
        key = json.dumps([os.path.normcase(os.path.abspath(fp)), args.get("offset"), args.get("limit"), args.get("pages")])
    except (TypeError, ValueError):
        return None
    return key, fp, [st.st_mtime_ns, st.st_size]


def view_of(response, args):
    """What the Read result put in context: {"first": line, "last": line, "partial": bool} or None if no file content came back.
    Claude Code's Read response: {"type": "text", "file": {"content", "numLines", "startLine", "totalLines", "truncatedByTokenCap"}}.
    Partial = cut by the token cap, or (no limit asked) the view ends before the file does (its 2000-line default page)."""
    f = response.get("file") if isinstance(response, dict) else None
    if not isinstance(f, dict) or not isinstance(f.get("content"), str):
        return None
    num = f.get("numLines") if isinstance(f.get("numLines"), int) else 0
    first = f.get("startLine") if isinstance(f.get("startLine"), int) and f.get("startLine") > 0 else 1
    total = f.get("totalLines") if isinstance(f.get("totalLines"), int) else 0
    last = first + num - 1 if num else 0
    asked_limit = isinstance(args, dict) and args.get("limit") is not None
    partial = bool(f.get("truncatedByTokenCap")) or (not asked_limit and bool(total) and bool(last) and last < total)
    eof = bool(last) and (not total or last >= total - 1)   # the view reached the end (its total counts a trailing empty row)
    return {"first": first, "last": last, "partial": partial, "eof": eof}


NONTEXT = ("image", "pdf", "notebook", "parts")


def kind_of(response):
    """'image' / 'pdf' / 'notebook' / 'parts' when a Read result put non-text content in context, else None."""
    if not isinstance(response, dict):
        return None
    t = response.get("type")
    if t in NONTEXT:
        return t
    f = response.get("file")
    if isinstance(f, dict):
        if f.get("base64"):
            return "image"
        if isinstance(f.get("cells"), list):
            return "notebook"
    return None


def kind_text(name, kind):
    return (f"🥛 Big Dad: {name} ({kind}) is already in your context and hasn't changed since. Use the copy you have; "
            "a different page range still goes through. " + C.READ_DENY_TAIL)


def asked_range(args):
    """(start, end) lines a Read asks for; end None = to its default page/the end. None for PDFs pages or odd input."""
    if not isinstance(args, dict) or args.get("pages") is not None:
        return None
    off, lim = args.get("offset"), args.get("limit")
    if (off is not None and not isinstance(off, int)) or (lim is not None and not isinstance(lim, int)) \
            or isinstance(off, bool) or isinstance(lim, bool):
        return None
    start = off if off and off > 0 else 1
    if lim is not None:
        return (start, start + lim - 1) if lim > 0 else None
    return start, None


def covering(seen, args, path_norm, stamp):
    """An earlier, delivered view of the same unchanged file whose lines wholly hold the asked range, or None."""
    want = asked_range(args)
    if want is None:
        return None
    start, end = want
    for k, v in seen.items():
        if not (isinstance(v, dict) and v.get("seen") and v.get("stamp") == stamp):
            continue
        try:
            kp = json.loads(k)[0]
        except (ValueError, TypeError, IndexError):
            continue
        if kp != path_norm or not isinstance(v.get("first"), int) or not isinstance(v.get("last"), int) or not v["last"]:
            continue
        if start < v["first"]:
            continue
        if (end is not None and end <= v["last"]) or (end is None and v.get("eof") and start <= v["last"]):
            return v
    return None


RANGE_READ_CAP = 4_000_000            # bytes: a bigger file is priced at the whole earlier copy


def range_tokens(view, args, stamp, fp=None):
    """Tokens of the asked range: its own lines read from the (unchanged) file, size / 4, never under the line share
    of the earlier view's tokens. A file that can't be sliced cheaply is priced at the whole earlier copy."""
    tok = copy_tokens(view, stamp)
    want = asked_range(args)
    first, last = view.get("first"), view.get("last")
    if not want or not isinstance(first, int) or not isinstance(last, int) or last < first:
        return tok
    end = min(want[1] if want[1] is not None else last, last)
    start = max(want[0], first)
    lines = max(1, end - start + 1)
    share = tok * lines // (last - first + 1)
    try:
        if not fp or os.path.getsize(fp) > RANGE_READ_CAP:
            return tok
        with open(fp, "rb") as f:
            body = f.read().decode("utf-8", "replace").splitlines(True)
        return max(share, len("".join(body[start - 1:end])) // 4)
    except (OSError, TypeError, ValueError):
        return tok


def cover_text(name, view, args):
    first, last = view["first"], view["last"]
    return (f"🥛 Big Dad: the lines you asked for of {name} are already in your context, inside lines {first}-{last} "
            "from an earlier Read, and the file hasn't changed since. Use that copy, or read lines outside it. "
            "If you truly lost it, ask for the same read again and it will go through. " + C.READ_DENY_TAIL)


def deny_text(name, view, args):
    sliced = isinstance(args, dict) and (args.get("offset") is not None or args.get("limit") is not None)
    first, last = (view or {}).get("first") or 1, (view or {}).get("last") or 0
    tail = (" If you truly lost it, ask for the same read again and it will go through. "
            "For an exact line count or a fact, use Bash (`wc -l`, grep) on the file rather than the numbered view: "
            "the view numbers a trailing empty line, so its total runs one over `wc -l`. " + C.READ_DENY_TAIL)
    if view and view.get("kind"):
        return kind_text(name, view["kind"])
    if view and view.get("partial"):
        return (f"🥛 Big Dad: your earlier Read of {name} is still in your context but PARTIAL (lines {first}-{last}), and the file "
                f"hasn't changed; the same Read would return the same slice. For the rest, Read with offset={last + 1} and a limit." + tail)
    if sliced and last:
        return (f"🥛 Big Dad: lines {first}-{last} of {name} are already in your context and the file hasn't changed since. "
                "Use that copy, or read a different offset/limit." + tail)
    return (f"🥛 Big Dad: {name} is already in your context in full and the file hasn't changed since. Use the copy you have "
            "(scroll back), or read only the lines you still need with offset/limit." + tail)


def copy_tokens(entry, stamp):
    """Tokens the earlier copy put in context: measured at PostToolUse (tok), else the file size / 4."""
    tok = entry.get("tok") if isinstance(entry, dict) else None
    if isinstance(tok, int) and not isinstance(tok, bool) and tok >= 0:
        return tok
    return (stamp[1] if isinstance(stamp, list) and len(stamp) == 2 and isinstance(stamp[1], int) else 0) // 4


def floor_lets_through(event, tokens):
    """True in a helper agent when a re-read of `tokens` is under the helper floor (see the docstring). Never in the
    main chat. Any error lets the read through (fail open: a missed deny costs one copy, a wrong one a retry)."""
    agent = C.dstr(event.get("agent_id"))
    if not agent:
        return False
    try:
        kind, n = C.helper_floor()
        if kind == "off":
            return False
        if kind == "tokens":
            return tokens < n
        import context_guard
        sid = C.dstr(event.get("session_id")) or "x"
        path = context_guard.agent_transcript(event, C.dstr(event.get("transcript_path")), sid, agent, workflows=True)
        seen = C.calls_and_size(path) if path else None      # a log that exists but can't be read raises: through
        if seen == "big":
            return False                       # a log past FLOOR_LOG_CAP: a long helper, keep the deny
        calls, size = seen or (0, 0)
        if not calls or not size:
            return tokens < C.FLOOR_FALLBACK
        p = C.FLOOR_RETRY_ODDS
        return tokens * (1 - p) * calls < p * size
    except Exception:
        return True


# --- R2: Bash/PowerShell dumps (same grammar as guild/savings/replay/replay.py::parse_dump) ---------------------------
SHELLS = ("Bash", "PowerShell")
DUMP_HINT = re.compile(r"(?i)\b(cat|head|tail|nl|bat|more|less|sed|type|get-content|gc)\b")
SAFE_VERBS = ("cd", "pushd", "echo", "printf", "set", "export")
FULL_VERBS = ("cat", "type", "get-content", "gc", "nl", "bat", "more", "less")
PS_VALUE_FLAGS = ("-totalcount", "-head", "-tail", "-first", "-last", "-encoding", "-readcount", "-delimiter")


def _toks(seg):
    return re.findall(r"\"[^\"]*\"|'[^']*'|\S+", seg)


def _unq(t):
    return t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t


def norm_path(p, cwd):
    """normcase(abspath()) of a shell path: /c/x -> C:/x, ~ -> home, relative -> against cwd. None when unsure."""
    p = _unq(str(p or "").strip())
    if not p or any(ch in p for ch in "*?[]{}"):
        return None
    if p.startswith("~"):
        if p != "~" and not p.startswith(("~/", "~\\")):
            return None
        p = os.path.expanduser("~") + p[1:]
    if os.name == "nt":
        p = p.replace("\\", "/")
        m = re.match(r"^/([a-zA-Z])(/.*)?$", p)
        if m:
            p = m.group(1) + ":" + (m.group(2) or "/")
        elif p.startswith("/"):
            return None                      # Git Bash's /tmp, /usr ...: not a Windows path we can be sure of
    if not os.path.isabs(p) or (os.name == "nt" and not re.match(r"^[a-zA-Z]:", p)):
        base = norm_path(cwd, None) if cwd else None
        if not base:
            return None
        p = os.path.join(base, p)
    return os.path.normcase(os.path.abspath(p))


def parse_dump(cmd, cwd, shell="Bash"):
    """A pure file dump -> [(path, range)], or None when the command does anything else (or we are unsure)."""
    if not isinstance(cmd, str) or not cmd.strip() or not DUMP_HINT.search(cmd):
        return None
    if shell != "PowerShell" and "\\" in cmd:
        return None                          # bash escapes: the path we would see is not the one bash opens
    clean = cmd.replace("2>/dev/null", "").replace("2>&1", "")
    if re.search(r"[<>`$(){}]", clean) or re.search(r"(?<!&)&(?!&)", clean):
        return None
    out = []
    for seg in (x.strip() for x in re.split(r"&&|\|\||;|\n", clean)):
        if not seg:
            continue
        head = _toks(seg)
        if head and head[0] in SAFE_VERBS:
            if "|" in seg:
                return None
            if head[0] in ("cd", "pushd"):
                if len(head) != 2 or head[1] == "-":
                    return None
                cwd = norm_path(head[1], cwd)
                if not cwd:
                    return None
            continue
        parts = [p.strip() for p in seg.split("|")]
        head = _toks(parts[0])
        if not head:
            return None
        verb = head[0].lower()
        if verb == "type" and shell != "PowerShell":
            return None                      # bash `type` describes a command
        rng, files = [verb], []
        if verb == "sed":
            if len(head) < 4 or head[1] != "-n" or not re.fullmatch(r"\d+(,\d+)?p", _unq(head[2])):
                return None
            rng = ["sed", _unq(head[2])]
            files = head[3:]
        elif verb in FULL_VERBS + ("head", "tail"):
            it = iter(head[1:])
            for t in it:
                lt = t.lower()
                if lt in ("-path", "-literalpath"):
                    files.append(next(it, ""))
                elif t.startswith("-"):
                    if lt in ("-f", "--follow", "-wait") or (verb in ("head", "tail") and lt.startswith("--")):
                        return None
                    rng.append(t)
                    if t in ("-n", "-c") or lt in PS_VALUE_FLAGS:
                        rng.append(next(it, ""))
                elif re.fullmatch(r"\d+", t) and verb in ("head", "tail"):
                    rng.append(t)
                else:
                    files.append(t)
        else:
            return None
        for p in parts[1:]:                  # only `| head` / `| tail` keep it a dump
            pt = _toks(p)
            if not pt or pt[0] not in ("head", "tail"):
                return None
            rng.append("|" + " ".join(pt))
        if not files:
            return None
        r = " ".join(rng)
        if r in FULL_VERBS:
            r = "FULL"
        for f in files:
            if not f or f.startswith("-"):
                return None
            np = norm_path(f, cwd)
            if not np:
                return None
            out.append((np, r))
    return out or None


def _stamp(p):
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return [st.st_mtime_ns, st.st_size] if os.path.isfile(p) else None


def bash_key(p, r):
    return json.dumps(["bash", p, r])


def bash_ok(response):
    """The dump came back: not an error, not interrupted, exit code 0 when one is given."""
    if isinstance(response, str):
        return bool(response)
    if not isinstance(response, dict):
        return False
    if response.get("interrupted") or response.get("is_error") or response.get("isError") or response.get("error"):
        return False
    for k in ("exitCode", "exit_code", "returnCode", "code"):
        if k in response and response[k] not in (0, None):
            return False
    if not C.dstr(response.get("stdout")) and C.dstr(response.get("stderr")):
        return False
    return True


def bash_after(event, state):
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    dump = parse_dump(args.get("command"), C.dstr(event.get("cwd")), event.get("tool_name"))
    if not dump or not bash_ok(event.get("tool_response")):
        return
    resp = event.get("tool_response")
    out = resp.get("stdout") if isinstance(resp, dict) else resp
    if isinstance(out, str) and "Big Dad shrink: showed" in out[-3000:]:
        return                                # the output shrink cut it: the file is NOT whole in context
    total = C.est_tokens(event.get("tool_response"))[0]
    stamps = [(p, r, _stamp(p)) for p, r in dump]
    sizes = sum(s[1] for _, _, s in stamps if s) or 1
    for p, r, stamp in stamps:                # each file's share of the output by its size, not an even split
        if stamp:
            tok = total if len(dump) == 1 else max(total * stamp[1] // sizes, stamp[1] // 4)
            put(state, bash_key(p, r), {"stamp": stamp, "seen": True, "tok": tok})


def bash_text(names, how):
    return (f"🥛 Big Dad: {names} is already in your context ({how}) and hasn't changed since. Use that copy. "
            "grep/wc/rg for a fact still go through, and so does a different range. "
            "Run the same command again if you truly lost it. " + C.READ_DENY_TAIL)


def bash_pre(event, state, standalone):
    if C.off("BIGDAD_BASH_GUARD"):
        return
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    dump = parse_dump(args.get("command"), C.dstr(event.get("cwd")), event.get("tool_name"))
    if not dump:
        return
    seen = load(state)
    hows, marks, tokens = [], [], 0
    for p, r in dump:
        stamp = _stamp(p)
        if stamp is None:
            return
        bk = bash_key(p, r)
        mine = seen.get(bk) if isinstance(seen.get(bk), dict) else {}
        if mine.get("stamp") == stamp and (mine.get("blocked") or mine.get("cover_blocked") == stamp):
            return                                   # asked again on purpose: through
        if mine.get("seen") and mine.get("stamp") == stamp:
            hows.append("same " + r + " output")
            tokens += copy_tokens(mine, stamp)
        else:
            rd = seen.get(json.dumps([p, None, None, None]))
            if not (isinstance(rd, dict) and rd.get("seen") and rd.get("stamp") == stamp and not rd.get("partial")
                    and not rd.get("kind")):
                return
            hows.append("in full from your Read")
            tokens += copy_tokens(rd, stamp)
        marks.append((bk, stamp))
    if floor_lets_through(event, tokens):
        return                                       # a helper's small re-dump: through (helper size floor)
    if C.learning():
        C.learn_seen("second_read")                  # learning first (2.0.6): counted, not blocked
        return

    def mark(cur):
        for bk, stamp in marks:
            e = cur.get(bk)
            if isinstance(e, dict) and e.get("seen") and e.get("stamp") == stamp:
                e["blocked"] = True
            else:
                cur[bk] = {"stamp": stamp, "cover_blocked": stamp}
    update(state, mark)
    names = ", ".join(dict.fromkeys(os.path.basename(p) or p for p, _ in dump))
    return C.emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": bash_text(names, hows[0]),
    }}, standalone)


def after(event):
    """PostToolUse: mark the read as in context (with what the view covered), so the next identical Read can be stopped."""
    if event.get("tool_name") in SHELLS:
        sid = C.dstr(event.get("session_id")) or "x"
        return bash_after(event, state_path(f"{sid}-{C.dstr(event.get('agent_id')) or 'main'}"))
    if event.get("tool_name") != "Read":
        return
    args = event.get("tool_input")
    k = read_key(args)
    if k is None:
        return
    key, _, stamp = k
    resp = event.get("tool_response")
    view = view_of(resp, args)
    if view is not None:
        entry = {"stamp": stamp, "seen": True, "first": view["first"], "last": view["last"], "partial": view["partial"],
                 "eof": view["eof"], "tok": C.est_tokens(resp)[0]}
    else:
        kind = kind_of(resp)
        if kind is None:
            return
        entry = {"stamp": stamp, "seen": True, "first": 1, "last": 0, "partial": False, "kind": kind,
                 "tok": C.est_tokens(resp)[0]}
    sid = C.dstr(event.get("session_id")) or "x"
    put(state_path(f"{sid}-{C.dstr(event.get('agent_id')) or 'main'}"), key, entry)


def main(event=None, standalone=True):
    if C.off("BIGDAD_READ_GUARD"):
        return
    event = C.read_event() if event is None else event
    if event is None:
        return
    sid = C.dstr(event.get("session_id")) or "x"
    state = state_path(f"{sid}-{C.dstr(event.get('agent_id')) or 'main'}")  # each agent has its own context
    hook = event.get("hook_event_name")
    if hook == "PreCompact":
        try:
            os.remove(state)
        except OSError:
            pass
        return
    if hook == "PostToolUse":
        return after(event)
    if event.get("tool_name") in SHELLS:
        return bash_pre(event, state, standalone)
    if event.get("tool_name") != "Read":
        return
    args = event.get("tool_input")
    k = read_key(args)
    if k is None:
        return  # missing or odd path: let Read report it
    key, fp, stamp = k
    seen = load(state)
    prev = seen.get(key)
    if (isinstance(prev, dict) and prev.get("seen") and prev.get("stamp") == stamp and not prev.get("blocked")
            and not floor_lets_through(event, copy_tokens(prev, stamp))):
        if C.learning():
            C.learn_seen("second_read")              # learning first (2.0.6): counted, not blocked
            return

        def mark(cur):
            e = cur.get(key)
            if not (isinstance(e, dict) and e.get("stamp") == stamp):
                return False
            e["blocked"] = True
        update(state, mark)
        return C.emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_text(os.path.basename(fp) or fp, prev, args),
        }}, standalone)
    mine = prev if isinstance(prev, dict) else {}
    if not mine.get("seen") and mine.get("cover_blocked") != stamp:
        try:
            path_norm = json.loads(key)[0]
        except (ValueError, TypeError, IndexError):
            path_norm = None
        cov = covering(seen, args, path_norm, stamp) if path_norm else None
        if cov is not None and floor_lets_through(event, range_tokens(cov, args, stamp, fp)):
            cov = None                                  # a helper's small re-read: through (helper size floor)
        if cov is not None and C.learning():
            C.learn_seen("second_read")              # learning first (2.0.6): counted, not blocked
            return
        if cov is not None:
            put(state, key, {"stamp": stamp, "cover_blocked": stamp}, keep_seen=True)   # asked again on purpose: through
            return C.emit({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": cover_text(os.path.basename(fp) or fp, cov, args),
            }}, standalone)
    if not (isinstance(prev, dict) and prev.get("seen") and prev.get("stamp") == stamp):
        put(state, key, {"stamp": stamp}, keep_seen=True)
    try:                                            # big whole-file Read: outline first (helpers by default), once per file
        why = __import__("outline").check(event)
    except Exception:
        why = None
    if why:
        return C.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": why}}, standalone)
    try:                                            # main chat: big first whole read goes through with a note (2.0.1)
        tip = __import__("outline").big_note(event)
    except Exception:
        tip = None
    if tip:
        return C.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": tip}}, standalone)


if __name__ == "__main__":
    main()
