"""Shared bits for Big Dad's hooks: safe event reading, state paths, the user profile.

Hooks must never crash or block: bad stdin, odd field types and missing files all end in a
quiet exit. Big Dad's own knobs come from ~/.claude/bigdad/profile.json (written by
`bigdad.py setup`), with environment variables on top; global defaults when neither exists.
"""
import base64, json, os, re, stat, struct, sys, tempfile, time

DEFAULT_GUARD_AT = 130_000
# The read guard's deny reasons all end in READ_DENY_TAIL (read_guard.deny_text uses it), and the receipt
# and versus count a denied Read by it (is_read_deny). One constant, so the deny and the count can't drift.
READ_DENY_TAIL = "This note is about this one read only."
READ_DENY_OLD = "Big Dad: this exact read is already in your context"   # the deny text before v1.5, in older logs


def is_read_deny(text):
    """True when a tool result's text is one of Big Dad's read-guard deny reasons (any version, partial included)."""
    return isinstance(text, str) and "Big Dad" in text and (READ_DENY_TAIL in text or READ_DENY_OLD in text)
IMG_TOKENS = 1_600      # an image once Claude has scaled it (docs: ~width*height/750, about 1,600 at most)


def image_tokens(block):
    """Tokens for one image content block: width*height/750 when the header says (PNG, GIF, JPEG),
    capped at IMG_TOKENS because Claude scales bigger images down; IMG_TOKENS when unknown.
    Never the base64 length: a screenshot costs ~1.6k tokens, not the ~150k its base64 looks like."""
    data = ""
    for src in (block.get("source"), block.get("file"), block):
        if isinstance(src, dict):
            data = dstr(src.get("data")) or dstr(src.get("base64"))
            if data:
                break
    w = h = 0
    try:
        head = base64.b64decode(data[:6000] + "==") if data else b""
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            w, h = struct.unpack(">II", head[16:24])
        elif head[:4] == b"GIF8" and len(head) >= 10:
            w, h = struct.unpack("<HH", head[6:10])
        elif head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head) and head[i] == 0xFF:
                marker, seg = head[i + 1], struct.unpack(">H", head[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    break
                i += 2 + seg
    except (ValueError, TypeError, struct.error):
        w = h = 0
    if w and h:
        return max(1, min(IMG_TOKENS, (w * h) // 750))
    return IMG_TOKENS


def est(obj, _depth=0):
    """(chars-equivalent, image count) of any tool payload: text by length, every image block by
    its token estimate x 4, never by its base64. chars // 4 is the token estimate."""
    if isinstance(obj, str):
        return len(obj), 0
    if isinstance(obj, dict):
        if obj.get("type") == "image":
            return image_tokens(obj) * 4, 1
        chars = imgs = 0
        for k, v in obj.items():
            c, i = est(v, _depth + 1) if _depth < 8 else (len(str(v)), 0)
            chars += c + len(str(k)) + 4
            imgs += i
        return chars, imgs
    if isinstance(obj, list):
        chars = imgs = 0
        for v in obj:
            c, i = est(v, _depth + 1) if _depth < 8 else (len(str(v)), 0)
            chars += c + 2
            imgs += i
        return chars, imgs
    if obj is None:
        return 0, 0
    try:
        return len(json.dumps(obj)), 0
    except (TypeError, ValueError):
        return len(str(obj)), 0


def est_tokens(obj):
    """(tokens, kind) for a tool result: kind is 'image' when it holds image blocks."""
    chars, imgs = est(obj)
    return chars // 4, "image" if imgs else "text"


PATH_KEYS = ("transcript_path", "agent_transcript_path")


def read_event(need_state=True):
    """The hook event as a dict, or None for anything that isn't one (empty, text, null, list). Log paths are
    cleaned up once here (real_path), so every hook reads the same file whatever shape the path came in.
    None as well when need_state and the state folder is refused (state_problem): a hook with nowhere private to keep
    its state acts as if it got nothing (no note, no deny, no write). The session start passes need_state=False."""
    try:
        e = json.load(sys.stdin)
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(e, dict):
        return None
    if need_state and state_problem():
        return None
    bases = [os.path.dirname(real_path(dstr(e.get("transcript_path")))), dstr(e.get("cwd"))]
    for k in PATH_KEYS:
        if isinstance(e.get(k), str) and e[k]:
            r = real_path(e[k], bases)
            e[k] = r or ("" if e[k].strip()[:2] in (chr(92) * 2, "//") else e[k])
    return e


def real_path(p, bases=()):
    """The existing file a log path names, or "" if none. Accepts what Claude Code, Git Bash, WSL or a hand-typed
    path may send: quotes or spaces around it, file:// URLs, ~, Git Bash /c/..., either separator, and paths relative
    to the cwd or to any folder in bases. Never guesses beyond that: a path that names no file gives ""."""
    if not isinstance(p, str):
        return ""
    p = p.strip().strip('"').strip("'").strip()
    if not p:
        return ""
    if p.lower().startswith("file:"):
        from urllib.parse import unquote, urlparse
        u = urlparse(p)
        p = unquote(u.path)
        if os.name == "nt" and len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]                  # file:///C:/x -> C:/x
    if os.name == "nt" and (p[:2] in (chr(92) * 2, "//") or p[:2] == "/" + chr(92) or p[:2] == chr(92) + "/"):
        return ""                      # network share (\\server\x): Windows can hang seconds looking it up; logs are local
    # (on Linux/macOS a leading // is a local path, so it is looked up like any other)
    p = os.path.expanduser(p)
    tries = [p]
    if os.name == "nt":
        q = p.replace("\\", "/")
        if len(q) > 2 and q[0] == "/" and q[1].isalpha() and (len(q) == 2 or q[2] == "/"):
            tries.append(q[1].upper() + ":" + q[2:])               # Git Bash /c/x -> C:/x
        if q.startswith("/mnt/") and len(q) > 6 and q[5].isalpha() and q[6:7] in ("/", ""):
            tries.append(q[5].upper() + ":" + q[6:])               # WSL /mnt/c/x -> C:/x
    else:
        tries.append(p.replace("\\", "/"))                        # a Windows-style relative path on Linux/macOS
    for t in list(tries):
        if not os.path.isabs(t):
            tries.extend(os.path.join(b, t) for b in bases if b)
    for t in tries:
        try:
            if os.path.isfile(t):
                return os.path.normpath(t)
        except (OSError, ValueError):
            continue
    return ""


def same_file(a, b):
    try:
        return bool(a and b) and os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def dget(d, key, default=None):
    v = d.get(key) if isinstance(d, dict) else None
    return default if v is None else v


def dstr(v):
    return v if isinstance(v, str) else ""


def off(name):
    return os.environ.get(name, "").lower() in ("off", "0", "false")


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def bd_dir():
    """Big Dad's own folder. BIGDAD_HOME moves it (tests and replay benchmarks run real Claude Code without
    touching the real one)."""
    return os.environ.get("BIGDAD_HOME") or os.path.join(config_dir(), "bigdad")


# PRIVATE STATE FOLDER (2.0.9, outside review). Per-session state (guard, reads, watch flags, saved full outputs) used
# to sit in the system temp folder: /tmp on Linux and macOS, where other local users can read it, pre-create the names
# or plant symlinks. It now lives in bd_dir()/state (or BIGDAD_STATE_DIR), checked once per process (state_problem):
#   - a real folder: not a symlink, not a Windows junction or other reparse point, not a file;
#   - POSIX: owned by us (euid); a folder we own that group or others can open is set to 0700, through a handle
#     opened with O_NOFOLLOW, so a link swapped in after the check is not what gets the chmod;
#   - POSIX, the parent only (one stat): owned by us or root, and not writable by every user unless sticky (like /tmp).
#     Otherwise another user could rename our folder away and put their own in its place. Grandparents and the
#     group-write bit are not checked: a user's own folders are often group-writable (umask 002), and a parent we or
#     root own can only be changed by us or root.
#   - Windows: no ACL check here (a hook stays cheap). The default place inherits the user profile's ACL (you, SYSTEM,
#     Administrators); `health` reads the real ACL of whatever folder is in use.
# A refused folder: state_dir() gives NO_STATE, a path under this .py file, where every open, makedirs and listdir
# fails with OSError and glob finds nothing, so nothing is written anywhere; read_event() gives no event (the hook acts
# as if it was called with nothing: no note, no deny, no write). The session start and the status line say it in one
# line; `bigdad.py health` gives the reason.
NO_STATE = os.path.join(os.path.abspath(__file__), "no-state")
_state_cache = {}


def state_path():
    """Where Big Dad keeps per-session state, unchecked: BIGDAD_STATE_DIR, else bd_dir()/state. For display (health,
    tidy); everything that reads or writes state goes through state_dir()."""
    return os.path.abspath(os.environ.get("BIGDAD_STATE_DIR") or os.path.join(bd_dir(), "state"))


def check_state_dir(p):
    """'' when p is usable as Big Dad's private state folder (made if missing, tightened to 0700 on POSIX when we own
    it), else one plain line why not. A few syscalls, no subprocess: hooks call it once per process."""
    try:
        if not os.path.lexists(p):
            os.makedirs(p, mode=0o700, exist_ok=True)      # the mode is for the leaf; the umask can only take bits off
        st = os.lstat(p)
    except OSError as e:
        return f"{p} could not be made or read ({e.strerror or e.__class__.__name__})"
    if stat.S_ISLNK(st.st_mode):
        return f"{p} is a symlink (it could point anywhere)"
    if getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return f"{p} is a junction or other reparse point (it could point anywhere)"
    if not stat.S_ISDIR(st.st_mode):
        return f"{p} is not a folder"
    if os.name == "nt":
        return ""
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        return f"{p} could not be opened ({e.strerror or e.__class__.__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            return f"{p} is not a folder"
        if st.st_uid != os.geteuid():
            return f"{p} belongs to another user (uid {st.st_uid})"
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o700)
            if os.fstat(fd).st_mode & 0o077:
                return f"{p} is open to other users and could not be set to 0700"
    except OSError as e:
        return f"{p} is open to other users and could not be set to 0700 ({e.strerror or e.__class__.__name__})"
    finally:
        os.close(fd)
    parent = os.path.dirname(p)
    try:
        pst = os.stat(parent)
    except OSError as e:
        return f"its parent folder {parent} could not be read ({e.strerror or e.__class__.__name__})"
    if pst.st_uid not in (0, os.geteuid()):
        return f"its parent folder {parent} belongs to another user (uid {pst.st_uid}), who could swap it"
    if pst.st_mode & 0o002 and not pst.st_mode & stat.S_ISVTX:
        return f"its parent folder {parent} is writable by every user (and not sticky), so it could be swapped"
    return ""


def state_problem():
    """'' when the state folder is usable, else the reason it was refused (one line). Checked once per process and
    folder."""
    p = state_path()
    if p not in _state_cache:
        _state_cache[p] = check_state_dir(p)
    return _state_cache[p]


def state_dir():
    """Big Dad's private per-session state folder (bd_dir()/state, or BIGDAD_STATE_DIR), checked by state_problem().
    Refused: NO_STATE, where nothing can be written or found."""
    return NO_STATE if state_problem() else state_path()


def legacy_state_dir():
    """Where 2.0.8 and earlier kept state when BIGDAD_STATE_DIR was not set: the system temp folder. None when it is
    set (then the old place was that folder too). tidy and health look here for old leftovers."""
    return None if os.environ.get("BIGDAD_STATE_DIR") else tempfile.gettempdir()


def idle_line():
    """The one line the session start and the status line give when the state folder is refused."""
    return (f"🥛 Big Dad is idle: its state folder was refused: {state_problem()}. Its hooks write nothing and say "
            "nothing until it is fixed; `bigdad.py health` shows it.")


def profile():
    try:
        with open_wait(os.path.join(bd_dir(), "profile.json"), encoding="utf-8", errors="replace") as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        return {}


def guard_at():
    """Per-call token guide: env, else the profile (min(130k, ~65% of the real window)), else 130k."""
    try:
        v = int(os.environ.get("BIGDAD_GUARD_AT") or 0)
    except ValueError:
        v = 0
    if v > 0:
        return v
    v = dget(profile(), "guard_at", 0)
    return v if isinstance(v, int) and v > 0 else DEFAULT_GUARD_AT


def guard_source():
    """Where guard_at() took the guide from: "BIGDAD_GUARD_AT", "your profile" or "default"."""
    try:
        if int(os.environ.get("BIGDAD_GUARD_AT") or 0) > 0:
            return "BIGDAD_GUARD_AT"
    except ValueError:
        pass
    v = dget(profile(), "guard_at", 0)
    return "your profile" if isinstance(v, int) and v > 0 else "default"


NO_WINDOW = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW: a child of a windowless process gets none


def claude_version():
    """The installed Claude Code version, from `claude --version`; cached by the binary's path + mtime in
    bd_dir()/claude-version.json so it only runs again after an update. '' when claude isn't on PATH or doesn't
    answer in 3 s. BIGDAD_CLAUDE_BIN points at another binary (tests)."""
    import shutil, subprocess
    exe = os.environ.get("BIGDAD_CLAUDE_BIN") or shutil.which("claude") or ""
    try:
        mtime = os.path.getmtime(exe) if exe else 0
    except OSError:
        return ""
    cache_path = os.path.join(bd_dir(), "claude-version.json")
    cache = load_json(cache_path)
    if cache.get("exe") == exe and cache.get("mtime") == mtime and cache.get("version"):
        return cache["version"]
    try:
        cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]
        out = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=3,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+", out)
    if not m:
        return ""
    cache = load_json(cache_path)          # re-read: version_note may have written "told" meanwhile
    cache.update(exe=exe, mtime=mtime, version=m.group(0))
    try:
        os.makedirs(bd_dir(), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass
    return m.group(0)


def stop_file():
    return os.path.join(bd_dir(), "agent-stop-at")


def parse_cap(v):
    """A stop setting as ("tokens", n) or ("x", factor); ("off", 0) for anything else.
    100k / 1.5m / 150000 (>= 1000) = absolute tokens per call; 2 / 2x / 2.5 (1 <= x <= 20) = x the guide."""
    t = str(v if v is not None else "").strip().lower().replace(",", "").replace("_", "")
    mult, kind = 1, ""
    if t.endswith("x"):
        t, kind = t[:-1], "x"
    elif t[-1:] in ("k", "m"):
        t, mult, kind = t[:-1], (1000 if t[-1] == "k" else 1_000_000), "tokens"
    try:
        x = float(t) * mult
    except ValueError:
        return ("off", 0)
    if x != x or x in (float("inf"), float("-inf")):
        return ("off", 0)
    kind = kind or ("tokens" if x >= 1000 else "x")
    if kind == "tokens":
        return ("tokens", int(x)) if x >= 1000 else ("off", 0)
    return ("x", x) if 1.0 <= x <= 20 else ("off", 0)       # 500 is neither 500x nor 500 tokens: off


def stop_raw():
    """The hard-stop setting as written: BIGDAD_AGENT_STOP_AT wins, else the switch file (None = not set)."""
    v = os.environ.get("BIGDAD_AGENT_STOP_AT")
    if v is None:
        try:
            with open(stop_file(), encoding="utf-8", errors="replace") as f:
                v = f.read().strip()
        except OSError:
            return None
    return v


def stop_cap():
    """Opt-in hard stop for runaway subagents, in tokens per call (0 = off, the default). `agent-stop 100k` sets an
    absolute cap; `agent-stop 2` sets 2x the guide. BIGDAD_AGENT_STOP_AT wins, else the switch file. hooks/stop.sh
    checks the same two places in sh, so Python only starts on PreToolUse when the stop is on."""
    kind, x = parse_cap(stop_raw())
    if kind == "tokens":
        return x
    return int(x * guard_at()) if kind == "x" else 0


def stop_at():
    """The hard stop as a multiple of the guide (0 = off): an absolute cap is turned into its multiple."""
    kind, x = parse_cap(stop_raw())
    if kind == "x":
        return x
    return x / guard_at() if kind == "tokens" else 0.0


def stop_label(cap=None):
    """How the stop reads in a note: '100k cap' (absolute) or '2x-guide cap (260k)'."""
    cap = stop_cap() if cap is None else cap
    kind, x = parse_cap(stop_raw())
    return f"{x:g}x-guide cap ({cap // 1000:,}k)" if kind == "x" else f"{cap // 1000:,}k cap"


# HELPER SIZE FLOOR (1.7). In a helper agent a denied re-read is often asked again, and the retry is one more full call
# of that helper. Receipts bench rounds 1-4 (helper_agent, docs/a.md ~2.4k tokens): 18 of 26 denies were retried,
# each retry ~26-30k tokens, to save ~2k; real helper logs: 1 of 11 denies retried (a 3k-token file, ~31k for the
# retry). A deny pays when (1 - p) x copy x later calls > p x the helper's call size; with the retry odds p at 1 in 2
# (the two samples pooled, 19 of 37) and "calls so far" as the guess for calls left, the floor is call size / calls.
FLOOR_RETRY_ODDS = 0.5
FLOOR_FALLBACK = 4_000      # tokens: the floor when the helper's own log can't be read (the bench's 2.4k re-read is under it)
FLOOR_LOG_CAP = 8_000_000   # bytes: a bigger helper log is a long helper (many calls left): no floor


def helper_floor_raw():
    """The helper floor as written: BIGDAD_HELPER_FLOOR wins, else the `helper-floor` switch file (None = not set)."""
    v = os.environ.get("BIGDAD_HELPER_FLOOR")
    if v is None:
        try:
            with open(os.path.join(bd_dir(), "helper-floor"), encoding="utf-8", errors="replace") as f:
                v = f.read().strip()
        except (OSError, ValueError):
            return None
    return v


def helper_floor():
    """("auto", 0) the default: the floor follows the helper's size; ("tokens", n) a fixed floor; ("off", 0) every
    re-read of an unchanged file in context is denied, as before 1.7. Anything unreadable is "auto"."""
    t = str(helper_floor_raw() or "").strip().lower().replace(",", "").replace("_", "")
    if t in ("off", "0", "false", "no"):
        return ("off", 0)
    mult = 1
    if t[-1:] in ("k", "m"):
        t, mult = t[:-1], (1000 if t[-1] == "k" else 1_000_000)
    try:
        x = float(t) * mult if t and t.replace(".", "", 1).isdigit() else None
    except ValueError:
        x = None
    if x is None or x != x or x in (float("inf"),) or x < 1 or x > 10_000_000:
        return ("auto", 0)
    return ("tokens", int(x))


def calls_and_size(path, cap=FLOOR_LOG_CAP):
    """(API calls since the newest compact, newest call's input tokens) of a transcript. (0, 0) when no call came
    after the newest compact_boundary (the old size is gone). None when the file is missing; "big" when it is over
    cap. A file that exists but can't be read raises OSError (the caller fails open)."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError, ValueError):
        return None
    if size > cap:
        return "big"
    with open(path, "rb") as f:
        data = f.read().decode("utf-8", "replace")
    ids, last = set(), 0
    for line in data.splitlines():
        if '"compact_boundary"' in line and _is_boundary(line):
            ids, last = set(), 0
            continue
        if '"usage"' not in line:
            continue
        try:
            m = json.loads(line).get("message") or {}
        except (ValueError, AttributeError):
            continue
        u = m.get("usage") if isinstance(m, dict) else None
        if not isinstance(u, dict):
            continue
        n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                if isinstance(u.get(k), int))
        if n:
            ids.add(dstr(m.get("id")) or f"line{len(ids)}")
            last = n
    return len(ids), last


SOFT_AT = 0.8                           # the heads-up before the hard stop, as a share of the cap


def short_id(agent):
    return re.sub(r"[^A-Za-z0-9]", "", dstr(agent))[:8] or "agent"


def handoff_path(agent_type, agent):
    """Where a capped agent writes its handoff: ~/.claude/bigdad/handoffs/<agent_type>-<short id>.md."""
    kind = re.sub(r"[^A-Za-z0-9_.-]+", "-", dstr(agent_type)).strip("-.")[:40] or "agent"
    return os.path.join(bd_dir(), "handoffs", f"{kind}-{short_id(agent)}.md")


def pending_file(sid):
    """Notes waiting for the main chat's next hook (a capped agent's relaunch line), one JSON row each."""
    return os.path.join(state_dir(), f"bigdad-pending-{re.sub(r'[^A-Za-z0-9-]', '', dstr(sid))[:64] or 'x'}.jsonl")


def push_pending(sid, text):
    p = pending_file(sid)
    with lock(p):
        append_jsonl(p, {"ts": now(), "text": text})


def pop_pending(sid):
    """The waiting notes for this chat (oldest first), removed so each shows once. One stat when there are none."""
    p = pending_file(sid)
    if not os.path.exists(p):
        return []
    with lock(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            os.remove(p)
        except OSError:
            return []
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("text"), str) and d["text"] not in out:
            out.append(d["text"])
    return out


# PAUSE DEFAULT SWITCH. The pause used when there's no BIGDAD_PAUSE_AT and no `pause-at` file. 1.7: 0.0 = off,
# opt-in like agent-stop (nothing measured it; "never pushy"). `bigdad.py pause 2` is the yes.
PAUSE_DEFAULT = 0.0


def pause_at():
    """Main-chat pause point as a multiple of the guide (default PAUSE_DEFAULT; 0 = off). BIGDAD_PAUSE_AT wins, else
    the switch file `bigdad.py pause N|off` writes. The pause ends the current turn once per 1.5x step past it and
    tells the user how to realign (/compact or a fresh session); typing anything continues."""
    v = os.environ.get("BIGDAD_PAUSE_AT")
    if v is None:
        try:
            with open(os.path.join(bd_dir(), "pause-at"), encoding="utf-8", errors="replace") as f:
                v = f.read().strip()
        except OSError:
            return PAUSE_DEFAULT
    if str(v).strip().lower() in ("off", "0", ""):
        return 0.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return PAUSE_DEFAULT
    return x if x >= 1.0 else 0.0


def money(n):
    """Token count as the user sees it: 💲142k."""
    return f"💲{int(n) // 1000:,}k"


def tok(n):
    """A token count, short: 3.1M, 520k, 900."""
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n // 1000:,}k" if n >= 1000 else str(n)


def beat_file(sid):
    return os.path.join(state_dir(), f"bigdad-beat-{sid}")


def hooks_off(path, sid):
    """True when this chat has 3+ tool calls but the per-tool-call hook never wrote its heartbeat: the session
    started before Big Dad's hooks were installed or updated. Fix: /reload-plugins. False when the state folder is
    refused: the hooks run, they just have nowhere private to write the heartbeat."""
    if not path or state_problem() or os.path.exists(beat_file(sid or "x")):
        return False
    return len(re.findall(r'"type":\s*"tool_use"', tail_text(path, 400_000))) >= 3


def watch_flag(sid):
    """The flag `bigdad.py watch` (watch.py) writes for a session; the loop note runs only while it exists."""
    return os.path.join(state_dir(), f"bigdad-watch-{sid}.json")


def redirect_file(sid):
    """Redirect detector state for one chat: user-turn count, when it last fired, and the dropped plan (if any)
    that the next slim compact should drop. Lives in the state dir like the guard's files."""
    return os.path.join(state_dir(), f"bigdad-redirect-{sid}.json")


def boundary_after(path, offset):
    """True when the transcript has a compact_boundary row past byte `offset` (the compact really happened). Only a
    JSON row with type "system" and subtype "compact_boundary" counts: a tool result that merely contains the word
    (a grep of this very file) is not a compact."""
    if not path or not isinstance(offset, int) or offset < 0:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for line in f:
                if b'"compact_boundary"' not in line or b'"subtype"' not in line:
                    continue
                try:
                    row = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                    return True
    except OSError:
        return False
    return False


def settle_dropped(st, path=None, done=False):
    """Clear the dropped plan once a compact that was told to drop it has finished (done=True: SessionStart
    source "compact"; else a compact_boundary past the offset PreCompact recorded). True when it was cleared."""
    d = st.get("dropped")
    if not isinstance(d, dict) or "compact_at" not in d:
        return False
    if done or boundary_after(path, d.get("compact_at")):
        del st["dropped"]
        return True
    return False


def load_json(path):
    """A dict from a JSON file, {} for anything else (missing, bad, not a dict)."""
    try:
        with open_wait(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class lock:
    """Cross-process lock on <path>.lock (atomic O_EXCL create), so two copies of a hook (a plugin
    loaded twice) read-check-write one at a time. Waits up to `wait` s, treats a lock older than 10 s
    as stale, and never raises: a hook must not block, so if it can't lock, the body still runs."""

    def __init__(self, path, wait=1.5):
        self.lp = path + ".lock"
        self.wait = wait
        self.fd = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.lp), exist_ok=True)
        except OSError:
            return self
        end = time.time() + self.wait
        while True:
            try:
                self.fd = os.open(self.lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.lp) > 10:
                        os.remove(self.lp)
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    return self
                time.sleep(0.02)
            except OSError:
                return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
                os.remove(self.lp)
            except OSError:
                pass
        return False


def tail_text(path, n):
    """The last n bytes of a file as text ('' if unreadable)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def emit(out, standalone=True):
    """Hook output. Standalone (one hook = one process): print it, JSON for dicts, text as is. In-process
    (post_tool.py / pre_compact.py run several hooks on one event): return it for the caller to merge."""
    if standalone and out:
        text = (out if isinstance(out, str) else json.dumps(out)) + "\n"
        try:                           # UTF-8 bytes whatever the console code page (cp1252 can't print 🥛)
            sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.flush()
        except (AttributeError, ValueError):
            print(text, end="")
    return out


def run_parts(names, event):
    """Run several hook modules' main(event, standalone=False) in this process, each in its own try/except
    (one failing never blocks the others), and merge their outputs into one hook response.
    Dicts merge: systemMessage joined, additionalContext joined, a deny decision wins. Text (PreCompact) is joined."""
    outs = []
    for name in names:
        try:
            r = __import__(name).main(event, standalone=False)
        except Exception:
            r = None
        if r:
            outs.append(r)
            u = (r.get("hookSpecificOutput") or {}).get("updatedToolOutput") if isinstance(r, dict) else None
            if isinstance(u, dict) and isinstance(event, dict):
                event = dict(event, tool_response=u)    # later parts (watch) size what the model will really see
    if not outs:
        return None
    if all(isinstance(o, str) for o in outs):
        return "\n".join(outs)
    merged = {}
    for o in outs:
        if not isinstance(o, dict):
            continue
        if o.get("continue") is False:          # a pause from any part ends the turn
            merged["continue"] = False
            merged["stopReason"] = (merged.get("stopReason", "") + " " + (o.get("stopReason") or "")).strip()
        if o.get("systemMessage"):
            merged["systemMessage"] = (merged.get("systemMessage", "") + " " + o["systemMessage"]).strip()
        h = o.get("hookSpecificOutput") or {}
        if h:
            m = merged.setdefault("hookSpecificOutput", {})
            m.setdefault("hookEventName", h.get("hookEventName"))
            if h.get("additionalContext"):
                m["additionalContext"] = (m.get("additionalContext", "") + "\n" + h["additionalContext"]).strip()
            if isinstance(h.get("updatedToolOutput"), dict) and "updatedToolOutput" not in m:   # output shrink
                m["updatedToolOutput"] = h["updatedToolOutput"]
            if h.get("permissionDecision") and m.get("permissionDecision") != "deny":
                m["permissionDecision"] = h["permissionDecision"]
                m["permissionDecisionReason"] = h.get("permissionDecisionReason", "")
    return merged or None


def learning():
    """True while Big Dad learns first (2.0.6): bd_dir()/learning exists. One stat, no log read; the session start
    and the refresh (skills/bigdad/scripts/bd_learn.py) start and end it. BIGDAD_LEARN=off: acts at once."""
    return not off("BIGDAD_LEARN") and os.path.exists(os.path.join(bd_dir(), "learning"))


def learn_seen(kind):
    """Count one thing a hook saw but held while learning (numbers only)."""
    append_jsonl(os.path.join(bd_dir(), "learn-seen.jsonl"), {"k": kind})


def learned_note():
    """The one line saying learning is over, once: whoever removes bd_dir()/learned first shows it."""
    p = os.path.join(bd_dir(), "learned")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        os.remove(p)
    except OSError:
        return None
    return text or None


HOOK_WAIT = 0.2         # s: the most a hook waits out a held file, on saves only: hooks stay fast
COMMAND_WAIT = 2.5      # s: the most a bigdad.py command waits (patient()), on reads, saves and removes
HELD_BLOCKS = os.name == "nt"           # only Windows refuses a replace, remove or read of a file open elsewhere
_patient = False


class Held(PermissionError):
    """A file another process still had open when the wait ran out (Windows). An OSError, so every caller that
    handles one still does; bigdad.py says it in one line. what: "save", "read" or "remove"; waited: seconds."""
    what = "save"
    waited = 0.0


def patient():
    """bigdad.py commands (typed, or the detached startup refresh): wait a held file out for up to COMMAND_WAIT s,
    on reads and removes as well as saves. Hooks never call this: they keep HOOK_WAIT, on saves only."""
    global _patient
    _patient = True


def _budget(save):
    if _patient and HELD_BLOCKS:
        return COMMAND_WAIT
    return HOOK_WAIT if save else 0.0


def _pauses(budget):
    """0.02, 0.04, 0.06, 0.08 s (HOOK_WAIT is these four, as in 2.0.5), then 0.1 s each: `budget` s in all."""
    left, i = round(budget * 1000), 0
    while left > 0:
        i += 1
        ms = min(20 * i, 100, left)
        left -= ms
        yield ms / 1000


def _stuck(path, writing):
    """No wait helps: a folder where the file goes, or (writing) a read-only file."""
    return os.path.isdir(path) or (writing and os.path.exists(path) and not os.access(path, os.W_OK))


def _held(e, path, what, budget):
    h = Held(e.errno, e.strerror, path)
    h.what, h.waited = what, budget
    return h


def replace_file(tmp, dst):
    """os.replace(tmp, dst) for Big Dad's own JSON files. On Windows the replace fails while another process has dst
    open (the background refresh reading baseline.json, a hook reading profile.json): wait that out, and only then
    (a hook HOOK_WAIT, a command COMMAND_WAIT). Still failing: tmp removed, dst as it was, an OSError for the caller
    (Held when dst was there and held the whole wait)."""
    budget = _budget(True)
    pauses = _pauses(budget)
    while True:
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as e:   # WinError 5 / 32: dst open elsewhere without delete sharing
            pause = None if _stuck(dst, True) else next(pauses, None)
            if pause is None:
                _drop(tmp)
                if _stuck(dst, True) or not os.path.exists(dst):
                    raise
                raise _held(e, dst, "save", budget) from None
            time.sleep(pause)
        except OSError:
            _drop(tmp)
            raise


def remove_file(path):
    """os.remove for Big Dad's own files. A command waits out a file held open elsewhere (Windows) for up to
    COMMAND_WAIT s, then raises Held; a hook does not wait (as before)."""
    _waited(lambda: os.remove(path), path, True, "remove")


def open_wait(path, mode="r", **kw):
    """open(). A command waits out a file another program holds without sharing (Windows: a virus scan, a sync or
    backup tool) for up to COMMAND_WAIT s, then raises Held; a hook does not wait (as before)."""
    writing = any(c in mode for c in "wax+")
    return _waited(lambda: open(path, mode, **kw), path, writing, "save" if writing else "read")


def _waited(do, path, writing, what):
    budget = _budget(False)
    pauses = _pauses(budget)
    while True:
        try:
            return do()
        except PermissionError as e:
            if not budget or _stuck(path, writing) or not os.path.exists(path):
                raise
            pause = next(pauses, None)
            if pause is None:
                raise _held(e, path, what, budget) from None
            time.sleep(pause)


def _drop(p):
    try:
        os.remove(p)
    except OSError:
        pass


def append_jsonl(path, row):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _is_boundary(line):
    try:
        d = json.loads(line)
    except ValueError:
        return False
    return isinstance(d, dict) and d.get("type") == "system" and d.get("subtype") == "compact_boundary"


def last_call_since(path, offset, cap=2_000_000):
    """(input tokens, model) of the newest API call written past byte `offset`, reading only those bytes;
    (0, '') when none was. None when more than `cap` bytes were appended, or when the byte before `offset` isn't
    a line end (the file was rewritten, not appended to), or when a compact_boundary comes after the newest call in
    those bytes (the old size is gone): the caller reads the tail instead, and last_call says 0 for a compact."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if not isinstance(offset, int) or offset < 0 or offset > size or size - offset > cap:
                return None
            if offset:
                f.seek(offset - 1)
                if f.read(1) != b"\n":
                    return None
            f.seek(offset)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return None
    for line in reversed(lines):
        if '"compact_boundary"' in line and _is_boundary(line):
            return None
        if '"usage"' not in line:
            continue
        try:
            m = json.loads(line).get("message") or {}
        except (ValueError, AttributeError):
            continue
        u = m.get("usage") if isinstance(m, dict) else None
        if not isinstance(u, dict):
            continue
        n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                if isinstance(u.get(k), int))
        if n:
            return n, dstr(m.get("model"))
    return 0, ""


def last_call(path, spans=(200_000, 2_000_000)):
    """(input tokens, model) of the most recent API call in a transcript, read from its tail. (0, "") when the chat
    was compacted after that call: the old size is gone and no call has measured the new one yet (bench round 1:
    the first prompt after /compact got "every call re-sends 43k ... /compact now")."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            for span in spans:
                f.seek(max(0, size - span))
                lines = f.read().decode("utf-8", "replace").splitlines()
                if size > span:
                    lines = lines[1:]          # first line is probably cut in half
                for line in reversed(lines):
                    if '"compact_boundary"' in line and _is_boundary(line):
                        return 0, ""           # compacted since the last call: that size is gone, the new one unknown
                    if '"usage"' not in line:
                        continue
                    try:
                        m = json.loads(line).get("message") or {}
                    except (ValueError, AttributeError):
                        continue
                    u = m.get("usage") if isinstance(m, dict) else None
                    if not isinstance(u, dict):
                        continue
                    n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                            if isinstance(u.get(k), int))
                    if n:
                        return n, dstr(m.get("model"))
                if size <= span:
                    break
    except (OSError, TypeError, ValueError):
        pass
    return 0, ""
