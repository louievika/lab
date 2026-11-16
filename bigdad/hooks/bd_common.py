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
EVENT_CWD = ""          # the event's cwd, kept by read_event: the project the per-project guide is read from


def read_event(need_state=True):
    """The hook event as a dict, or None for anything that isn't one (empty, text, null, list). Log paths are
    cleaned up once here (real_path), so every hook reads the same file whatever shape the path came in.
    None as well when need_state and the state folder is refused (state_problem): a hook with nowhere private to keep
    its state acts as if it got nothing (no note, no deny, no write). The session start passes need_state=False.
    3.0.0 eleventh review: an event nested too deeply to parse (RecursionError) is not one either."""
    try:
        e = json.load(sys.stdin)
    except (ValueError, OSError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(e, dict):
        return None
    global EVENT_CWD
    EVENT_CWD = dstr(e.get("cwd"))
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


# thirty-sixth review: the characters shown() prints as '?': C0, DEL, C1, the line and paragraph separators and the
# bidi controls (embeddings, overrides, isolates)
_CONTROL = frozenset([chr(i) for i in range(0x20)] + [chr(i) for i in range(0x7f, 0xa0)]
                     + ["\u2028", "\u2029"] + [chr(i) for i in range(0x202a, 0x202f)]
                     + [chr(i) for i in range(0x2066, 0x206a)])


def shown(text):
    """Text from a log, a .meta.json, a state file, the profile or the environment as safe to print to the terminal
    (thirty-sixth review, the one rule): not a str gives ''; every control character (_CONTROL) is shown as '?', so an
    escape sequence can't reach the terminal; everything else, non-ASCII letters too, is kept. No cut."""
    return "".join("?" if c in _CONTROL else c for c in dstr(text))


def label(text):
    """A name where a row or a list expects one (a tool, a hook, a skill, a model, a kind): shown(text), or '?' when
    that is empty or text is not a str (thirty-seventh review: a non-str name printed blank). Free text keeps shown."""
    return shown(text) or "?"


def shown_env(raw):
    """Raw environment text as setup prints it (thirty-fourth review, in bd_setup; thirty-fifth review, here): cut to 40
    characters, then shown() (thirty-sixth review: control characters as '?', non-ASCII kept; was printable ASCII
    only). Not a str: ''."""
    return shown(dstr(raw)[:40])


# thirty-seventh review: the net at the door. Big Dad's own colours are exactly ESC [ digits-and-semicolons m (bd_money,
# bd_baseline, statusline); a newline is the only other control character it prints itself
SGR = re.compile("\033\\[[0-9;]*m")    # thirty-eighth review: the one copy here; _OUT and bd_money.ANSI are built on it
_OUT = re.compile(SGR.pattern + "|[" +"".join(re.escape(c) for c in sorted(_CONTROL - {"\n"})) + "]")


def safe_out(text):
    """Text on its way to the terminal, as the command entry point's stream filter writes it (thirty-seventh review):
    '\\n' and Big Dad's own SGR colour sequences (SGR) kept; every other character in _CONTROL ('\\t', '\\r', a lone
    ESC, OSC, other CSI, BEL, C1, U+2028/2029, the bidi controls) as '?'; the rest kept. Same length as text. One
    call sees one piece: a sequence split over two calls is not joined, so its ESC shows as '?'. Not a str: ''."""
    return _OUT.sub(lambda m: m.group() if len(m.group()) > 1 else "?", dstr(text))


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
#   - POSIX: owned by us (euid); the default folder, when we own it and group or others can open it, is set to 0700,
#     through a handle opened with O_NOFOLLOW, so a link swapped in after the check is not what gets the chmod.
#     A folder BIGDAD_STATE_DIR names is never chmodded (3.0.0 review): open to others, it is refused, and the
#     reason gives the chmod line to run yourself;
#   - POSIX, every folder on the way to it (3.0.0, outside audit): the threat is another local user who can write a
#     folder on the way, and so rename our folder (or a folder above it, or a link on the way) aside and put their own
#     in its place after the check. Root is trusted (it can do anything anyway). So each folder a name is looked up in,
#     from / down to the parent, following links the way the OS does (a link's own folder is checked, and so is its
#     target's), must be owned by us or root and not writable by group or others, unless sticky (like /tmp: there only
#     an entry's owner can rename it, and every entry on the way is checked to be ours or root's). Each link followed
#     must be ours or root's (in a sticky folder its owner could swap it). Group write is fine only for our own private
#     group (named like us, our primary group, no other member: the umask 002 setup of Fedora and others) on a folder
#     with no POSIX ACL (with one, the group bits are the ACL mask). Not seen: macOS ACLs (they are not in the mode).
#     Users who link ~/.claude or ~/.claude/bigdad into a folder they own keep working. In a user-namespace sandbox
#     that does not map host root, root shows as the overflow uid; that uid counts as root there (_sandbox_root).
#   - A missing folder: the folders that already exist on the way are judged first, and nothing is made under a
#     refused one; then the real parent is judged as above.
#   - The verdict is cached for one process (a hook call). That cannot hide a later swap by another user: a folder that
#     passed can only be changed by us or root, and only a passing folder is used.
#   - Windows: no ACL check here (a hook stays cheap). The default place inherits the user profile's ACL (you, SYSTEM,
#     Administrators); `health` reads the real ACL of whatever folder is in use (win_sddl, win_acl_reading). 3.0.0,
#     measured: reading it in a cold hook process adds ~9.5 ms (median of 20, 53.5 -> 63.0 ms), almost all of it the
#     ctypes import (the two Win32 calls are under 1 ms), over the ~5 ms a hook may add, so it stays in `health`.
# check_state_dir(p, fix=False, chosen=...) only looks (for `health` and a session start with BIGDAD_GUARD=off): it makes
# nothing and sets no mode.
# A refused folder: state_dir() gives NO_STATE, a path under this .py file, where every open, makedirs and listdir
# fails with OSError and glob finds nothing, so nothing is written anywhere; read_event() gives no event (the hook acts
# as if it was called with nothing: no note, no deny, no write; post_tool.py still runs the pause the user set, which
# needs only the log: context_guard.refused_pause). The session start and the status line say it in one
# line; `bigdad.py health` gives the reason.
NO_STATE = os.path.join(os.path.abspath(__file__), "no-state")
_state_cache = {}


def state_path():
    """Where Big Dad keeps per-session state, unchecked: BIGDAD_STATE_DIR, else bd_dir()/state. For display (health,
    tidy); everything that reads or writes state goes through state_dir()."""
    return os.path.abspath(os.environ.get("BIGDAD_STATE_DIR") or os.path.join(bd_dir(), "state"))


TIGHTEN = "the hooks set it to 0700 when they next run"


def _private_group(gid, d):
    """True when gid is our own user-private group (named like us, our primary group, nobody else in it) and d has no
    POSIX ACL (then the group bits are the ACL mask, not the group's). Only asked about a group-writable folder."""
    try:
        import grp, pwd
        me, g = pwd.getpwuid(os.geteuid()), grp.getgrgid(gid)
        if gid != me.pw_gid or g.gr_name != me.pw_name or set(g.gr_mem) - {me.pw_name}:
            return False
        if "system.posix_acl_access" in getattr(os, "listxattr", lambda _: ())(d):
            return False
        # On a directory service with enumeration off this sees local accounts only: a user-private group is a naming
        # convention, and trusting it is the residual trust here.
        return not any(u.pw_gid == gid and u.pw_uid != me.pw_uid for u in pwd.getpwall())
    except (ImportError, KeyError, OSError):
        return False


_ns_owner = []


def _sandbox_root():
    """The kernel overflow uid when we run in a user namespace that does not map host root, else None.
    There (bubblewrap, `unshare -U`: Claude Code's Linux sandbox runs Bash-tool commands, so `bigdad.py`, under
    bubblewrap) every file owned by an uid not mapped in, root's / and /home too, shows as the overflow uid (65534), and
    "ours or root's" would refuse every path. So that uid is trusted like root, only when uid 0 outside is not mapped
    in, the overflow uid is not mapped in either (no real user inside shows as it), and it is not us. What is trusted:
    a process in the sandbox cannot tell host root from another host user, and the path to your own home running
    through another ordinary user's folder is not a normal setup (the hooks themselves run outside the sandbox and
    check the same path with the real owners). The mode rules still hold. Outside a user namespace uid 0 is mapped, so
    an overflow-uid owner is refused like any other user; an unreadable or odd uid_map also gives None."""
    if not _ns_owner:
        uid = None
        try:
            with open("/proc/self/uid_map") as f:
                rows = [tuple(int(n) for n in r.split()) for r in f if r.strip()]
            with open("/proc/sys/kernel/overflowuid") as f:
                over = int(f.read())
            if (not any(o <= 0 < o + n for _, o, n in rows) and not any(i <= over < i + n for i, _, n in rows)
                    and over != os.geteuid()):                 # a row that is not 3 numbers: ValueError, None
                uid = over
        except (OSError, ValueError):
            pass
        _ns_owner.append(uid)
    return _ns_owner[0]


def _trusted(uid):
    """We, root, or (in a user namespace without host root) the overflow uid that root shows as there."""
    return uid in (0, os.geteuid()) or uid == _sandbox_root()


def _folder_problem(d, st):
    """'' when only we or root can change what is in folder d (lstat st), else why not."""
    if not stat.S_ISDIR(st.st_mode):
        return f"{d} is not a folder"
    if not _trusted(st.st_uid):
        return f"{d} belongs to another user (uid {st.st_uid}), who could swap what is in it"
    if st.st_mode & stat.S_ISVTX:
        return ""
    if st.st_mode & 0o002:
        return f"{d} is writable by every user (and not sticky), so what is in it could be swapped"
    if st.st_mode & 0o020 and not _private_group(st.st_gid, d):
        return f"{d} is writable by its group (gid {st.st_gid}, not sticky), so another member could swap what is in it"
    return ""


def path_problem(d):
    """'' when every folder a name is looked up in on the way to folder d (from /, following links as the OS does, so
    a link's own folder and its target's) and d itself pass _folder_problem, and every link followed is ours or
    root's; else why not. POSIX only."""
    todo = [n for n in reversed(os.path.abspath(d).split(os.sep)) if n]
    cur, seen, hops = os.sep, set(), 0
    while True:
        if cur not in seen:
            seen.add(cur)
            why = _folder_problem(cur, os.lstat(cur))
            if why:
                return why
        if not todo:
            return ""
        name = todo.pop()
        if name == ".":
            continue
        if name == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, name)
        st = os.lstat(nxt)
        if not stat.S_ISLNK(st.st_mode):
            cur = nxt
            continue
        hops += 1
        if hops > 40:
            raise OSError(40, "too many levels of links")
        if not _trusted(st.st_uid):
            return f"{nxt} is a link owned by another user (uid {st.st_uid}), who could point it elsewhere"
        to = os.readlink(nxt)
        if to.startswith(os.sep):
            cur = os.sep
        todo.extend(n for n in reversed(to.split(os.sep)) if n)


def check_state_dir(p, fix=True, *, chosen):
    """'' when p is usable as Big Dad's private state folder, else one plain line why not. Made if missing; on POSIX,
    one we own that group or others can open is tightened to 0700, unless chosen is true (p is the folder the user
    named in BIGDAD_STATE_DIR; state_problem passes it): then it keeps its mode and is refused with the chmod line.
    chosen is keyword-only with no default, so no caller can leave it out.
    A few syscalls, no subprocess: hooks call it once per process.
    fix=False only looks (health): a missing folder is '' when the hooks could make it, one we own that others can
    open gives a line ending in TIGHTEN, and nothing is made or changed."""
    try:
        if not os.path.lexists(p):
            if os.name != "nt":                # 3.0.0: judge what is already there before anything is made in it
                top = os.path.dirname(os.path.abspath(p))
                while not os.path.lexists(top):
                    top = os.path.dirname(top)
                why = path_problem(top)
                if why:
                    return f"{why} (on the way to {p})"
            if not fix:
                return ""
            os.makedirs(p, mode=0o700, exist_ok=True)      # the mode is for the leaf; the umask can only take bits off
        st = os.lstat(p)
    except OSError as e:
        return f"{p} could not be made or read ({e.strerror or e.__class__.__name__})"
    why = entry_problem(st)
    if why:
        return f"{p} {why}"
    if not stat.S_ISDIR(st.st_mode):
        return f"{p} is not a folder"
    if os.name == "nt":
        return ""
    parent = os.path.dirname(p)
    try:
        why = path_problem(parent)
    except OSError as e:
        return f"a folder on the way to {p} could not be read ({e.strerror or e.__class__.__name__})"
    if why:
        return f"{why} (on the way to {p})"
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
            if chosen:
                import shlex           # 3.0.0 review: a folder you chose keeps its mode; you change it, or not
                return (f"{p} is open to other users (mode {stat.S_IMODE(st.st_mode):o}) and Big Dad does not change "
                        f"a folder BIGDAD_STATE_DIR names; to use it, run: chmod 700 {shlex.quote(p)}")
            if not fix:
                return f"{p} is open to other users (mode {stat.S_IMODE(st.st_mode):o}); {TIGHTEN}"
            os.fchmod(fd, 0o700)
            if os.fstat(fd).st_mode & 0o077:
                return f"{p} is open to other users and could not be set to 0700"
    except OSError as e:
        return f"{p} is open to other users and could not be set to 0700 ({e.strerror or e.__class__.__name__})"
    finally:
        os.close(fd)
    return ""


# Windows ACL entries (SDDL): every allow type (plain, object, callback, callback object), and the trustees that may
# open a Big Dad folder without a finding: SYSTEM, Administrators, the owner and creator-owner placeholders (and you).
WIN_ALLOW = {"A", "OA", "XA", "ZA"}
WIN_FINE = {"SY", "BA", "OW", "CO"}


def win_acl_reading(sddl, me):
    """What a folder's Windows ACL (an SDDL string holding its DACL, "D:...") says about who may open it, as (state,
    trustees):
      ("open", [])        a null DACL (D:NO_ACCESS_CONTROL, or no DACL part at all): no ACL, everyone gets in;
      ("nobody", [])      an empty DACL, or deny entries only: nobody gets in, you neither;
      ("private", [])     allow entries for you, SYSTEM, Administrators, owner and creator-owner only;
      ("others", [...])   the other trustees with an allow entry of any type (SDDL aliases like "BU", or SIDs);
      ("unread", [])      anything else: never read as private.
    Deny entries are not weighed against allows (an allow is named even if a deny would block it): this only says
    who is let in by an entry, and errs toward naming them. Pure (strings in, no system calls), so tests feed it."""
    if not isinstance(sddl, str) or not sddl.startswith(("O:", "G:", "D:", "S:")):
        return "unread", []
    if "D:" not in sddl:                   # we ask for the DACL: none present means everyone is let in
        return "open", []
    dacl = sddl.split("D:", 1)[1]
    flags = re.match(r"(?:P|AI|AR|NO_ACCESS_CONTROL)*", dacl).group()
    if "NO_ACCESS_CONTROL" in flags:
        return "open", []
    aces, depth, quote, start = [], 0, False, 0
    for i in range(len(flags), len(dacl)):  # top-level (...) entries; a callback's condition nests () and "quotes"
        ch = dacl[i]
        if quote:
            quote = ch != '"'
        elif ch == '"' and depth:
            quote = True
        elif ch == "(":
            depth += 1
            start = i + 1 if depth == 1 else start
        elif ch == ")" and depth:
            depth -= 1
            if not depth:
                aces.append(dacl[start:i])
        elif not depth:
            if dacl[i:i + 2] == "S:":
                break                      # the SACL starts
            return "unread", []
    else:
        if depth or quote:
            return "unread", []
    others = []
    for ace in aces:
        f = ace.split(";", 6)
        if len(f) < 6 or not f[5]:
            return "unread", []
        if f[0] in WIN_ALLOW and f[5] not in WIN_FINE and f[5] != me and f[5] not in others:
            others.append(f[5])
    if others:
        return "others", others
    return ("private", []) if any(ace.split(";", 1)[0] in WIN_ALLOW for ace in aces) else ("nobody", [])


def win_sddl(path):
    """Windows only: (SDDL of path's owner and DACL, your user SID), or None when either can't be read (or not
    Windows). Read with the Win32 API (GetNamedSecurityInfoW -> SDDL), so names are not localized and nothing is
    started. ctypes is imported here, not at the top: its import alone costs a hook ~10 ms."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        vp = ctypes.c_void_p
        k32.LocalFree.argtypes, k32.LocalFree.restype = [vp], vp
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.POINTER(vp)] * 5
        adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            vp, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, vp, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.ConvertSidToStringSidW.argtypes = [vp, ctypes.POINTER(wintypes.LPWSTR)]
        OWNER, DACL = 0x1, 0x4
        psd = vp()
        if adv.GetNamedSecurityInfoW(path, 1, OWNER | DACL, None, None, None, None, ctypes.byref(psd)):
            return None                                   # 1 = SE_FILE_OBJECT; nonzero = an error code
        try:
            s = wintypes.LPWSTR()
            if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(psd, 1, OWNER | DACL, ctypes.byref(s), None):
                return None
            sddl = s.value or ""
            k32.LocalFree(ctypes.cast(s, vp))
        finally:
            k32.LocalFree(psd)
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x8, ctypes.byref(tok)):   # TOKEN_QUERY
            return None
        try:
            n = wintypes.DWORD()
            adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(n))                     # 1 = TokenUser
            buf = ctypes.create_string_buffer(n.value or 1)
            if not adv.GetTokenInformation(tok, 1, buf, n, ctypes.byref(n)):
                return None
            ssid = wintypes.LPWSTR()
            if not adv.ConvertSidToStringSidW(ctypes.cast(buf, ctypes.POINTER(vp))[0], ctypes.byref(ssid)):
                return None
            me = ssid.value
            k32.LocalFree(ctypes.cast(ssid, vp))
        finally:
            k32.CloseHandle(tok)
    except (OSError, AttributeError, ValueError):
        return None
    return sddl, me


def state_problem(fix=True):
    """'' when the state folder is usable, else the reason it was refused (one line). Checked once per process and
    folder. fix=False only looks (see check_state_dir)."""
    p = state_path()
    if (p, fix) not in _state_cache:
        _state_cache[(p, fix)] = check_state_dir(p, fix, chosen=bool(os.environ.get("BIGDAD_STATE_DIR")))
    return _state_cache[(p, fix)]


def state_dir(fix=True):
    """Big Dad's private per-session state folder (bd_dir()/state, or BIGDAD_STATE_DIR), checked by state_problem().
    Refused: NO_STATE, where nothing can be written or found."""
    return NO_STATE if state_problem(fix) else state_path()


def legacy_state_dir():
    """Where 2.0.8 and earlier kept state when BIGDAD_STATE_DIR was not set: the system temp folder. None when it is
    set (then the old place was that folder too). tidy and health look here for old leftovers."""
    return None if os.environ.get("BIGDAD_STATE_DIR") else tempfile.gettempdir()


def idle_line():
    """The one line the session start and the status line give when the state folder is refused."""
    return (f"🥛 Big Dad is idle: its state folder was refused: {state_problem()}. Its hooks write nothing and say "
            "nothing until it is fixed (a pause you set still acts); `bigdad.py health` shows it.")


def profile():
    try:
        with open_wait(os.path.join(bd_dir(), "profile.json"), encoding="utf-8", errors="replace") as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


PROJECT_FILE = os.path.join(".claude", "bigdad.json")     # <project>/.claude/bigdad.json {"guard_at": N}: Big Dad's own
PROJECT_DEPTH = 32


def project_file(cwd=None):
    """The <project>/.claude/bigdad.json that applies in cwd (the event's cwd, else this process's): the nearest one
    walking up, never in or above the home folder (~/.claude is the user's Claude folder, not a project). '' if none.
    Stat calls only: no process, no log read."""
    d = os.path.abspath(cwd or EVENT_CWD or os.getcwd())
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    cfg = os.path.normcase(os.path.abspath(config_dir()))
    for _ in range(PROJECT_DEPTH):
        if os.path.normcase(d) == home:
            return ""
        if os.path.normcase(os.path.join(d, ".claude")) != cfg:
            p = os.path.join(d, PROJECT_FILE)
            if os.path.isfile(p):
                return p
        up = os.path.dirname(d)
        if up == d:
            return ""
        d = up
    return ""


def project_guide(cwd=None):
    """(guide, file) from this project's .claude/bigdad.json, (0, file or '') when it sets none."""
    p = project_file(cwd)
    v = dget(load_json(p), "guard_at", 0) if p else 0
    return (v if guide_ok(v) else 0), p


NUM_TOP = 10 ** 12      # the top for any count or time read back from a file: token counts and epoch seconds fit
GUIDE_TOP = NUM_TOP     # far past any window: a bigger guide reads as unset


def sane(v, default=0, whole=False):
    """A usable count or time read back from a state or cache file (thirty-second review): an int or float, not a
    bool, finite, 0 <= v <= NUM_TOP (whole=True: an int only); anything else -> default. A 400-digit int raised
    OverflowError the moment it met a float; an infinite, NaN or huge count silenced a note for the whole chat."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (whole and not isinstance(v, int)):
        return default
    return v if 0 <= v <= NUM_TOP else default   # NaN fails both tests; int vs float compares exactly


def sane_ok(v, whole=False):
    """True when sane() would keep v (thirty-third review: the one predicate for `sane(v, None, whole) is not None`)."""
    return sane(v, None, whole) is not None


def guide_ok(v):
    """A guide from a file: an int (not bool) above 0 and up to GUIDE_TOP. Thirty-first review: a 400-digit
    guard_at raised OverflowError wherever the guide met a float (0.7 * guide)."""
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= GUIDE_TOP


def env_guide():
    """BIGDAD_GUARD_AT as a usable guide (a whole number that passes guide_ok), else None: unset, empty, not a whole
    number, past Python's int digit limit, 0, negative or above GUIDE_TOP. Thirty-third review: the one reader of
    the variable, for _guide, agent_guide and bd_setup (two of them took any value above 0, or any int, as set)."""
    try:
        v = int(os.environ.get("BIGDAD_GUARD_AT") or 0)
    except ValueError:                  # also a value past Python's int digit limit
        return None
    return v if guide_ok(v) else None


def _guide():
    """(guide, source): env BIGDAD_GUARD_AT, else this project's own, else the profile, else the default. Every
    source passes guide_ok here, in one place (thirty-second review: the env value had no top)."""
    for get, source in ((lambda: env_guide() or 0, "BIGDAD_GUARD_AT"), (lambda: project_guide()[0], "this project"),
                        (lambda: dget(profile(), "guard_at", 0), "your profile")):
        v = get()
        if guide_ok(v):
            return v, source
    return DEFAULT_GUARD_AT, "default"


def guard_at():
    """Per-call token cost guide (not a limit): env, else this project's (`bigdad.py guide N`), else the profile
    (min(130k, ~65% of the real window)), else 130k."""
    return _guide()[0]


def guard_source():
    """Where guard_at() took the guide from: "BIGDAD_GUARD_AT", "this project", "your profile" or "default"."""
    return _guide()[1]


GUARD_SHARE = 0.65                      # the guide's share of the window (bd_setup: min(130k, 65% of the window))


def model_window(model):
    """Context window Claude Code gives a model id by default (docs, model-config): 1M for the Fable models,
    Sonnet 5+, Haiku 5.5+, Opus 4.7+ and any [1m] variant; else 200k. CLAUDE_CODE_DISABLE_1M_CONTEXT=1 keeps every
    session at 200k. The same rule as bd_setup.model_window, here so a hook needs no script import."""
    if os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT") == "1":
        return 200_000
    m = model or ""
    if "[1m]" in m:
        return 1_000_000
    f = re.match(r"claude-([a-z]+)", m)
    fam = f.group(1) if f else ""
    v = re.search(r"-(\d+)-(\d+)", m)
    ver = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    if fam == "fable" or (fam == "sonnet" and ver >= (5, 0)) or (fam == "haiku" and ver >= (5, 5)) or (fam == "opus" and ver >= (4, 7)):
        return 1_000_000
    return 200_000


def agent_guide(path, limit=None):
    """3.0.0: a helper's cost guide follows the window it really has, by the main guide's 65% rule: a helper whose
    model has the 1M window gets 65% of it (650k), not the main chat's 130k (a 1M helper was told to stop at ~20% of
    its window). A 200k helper keeps the main guide. BIGDAD_GUARD_AT, the user's own number, is kept as set (only a
    usable one: env_guide, thirty-third review); a log with no model id keeps the main guide too."""
    limit = guard_at() if limit is None else limit
    if env_guide() is not None or not path:
        return limit
    found = re.findall(r'"model":\s*"(claude-[^"]+)"', tail_text(path, 60_000))
    w = model_window(found[-1]) if found else 0
    return max(limit, int(GUARD_SHARE * w)) if w > 200_000 else limit


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
        except (ValueError, AttributeError, RecursionError):
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
        except (ValueError, RecursionError):
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
                except (ValueError, RecursionError):
                    continue
                if isinstance(row, dict) and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
                    return True
    except (OSError, OverflowError, ValueError):   # thirty-first review: an offset too big for seek raised
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
    except (OSError, ValueError, RecursionError):
        return {}


def load_json_now(path):
    """load_json with no waiting, even under patient() (twenty-fourth review). For files read only to show something
    (an agent's .meta.json in `receipt`), where a held file is {} at once, not a ~2.5 s wait. Twenty-fifth review: opened
    with O_NONBLOCK and read only when fstat says a regular file, so a FIFO (which a plain open waits on forever on
    POSIX) or a directory is {} at once; links are followed as before. bd_models.meta_of is the same code on purpose."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
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


def pause_steps(budget):
    """0.02, 0.04, 0.06, 0.08 s (HOOK_WAIT is these four, as in 2.0.5), then 0.1 s each: `budget` s in all.
    Public: bd_isolation's open_wait uses it too."""
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
    pauses = pause_steps(budget)
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
    pauses = pause_steps(budget)
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
    except (ValueError, RecursionError):
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
        except (ValueError, AttributeError, RecursionError):
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
                    except (ValueError, AttributeError, RecursionError):
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


# 3.0.0 WHY A HELPER QUIT. When a call fails (usage limit, expired login, no credits, lost connection) Claude Code ends
# the agent and writes one synthetic assistant row to its log: model "<synthetic>", isApiErrorMessage true, the error in
# "apiError"/"error", the reset time in apiErrorParams.rate_limit_info / quotaLimits ("resetsAt", epoch seconds). Seen in
# this PC's logs 2026-11-14: usage_limit_reached (5-hour and weekly), model_requires_usage_credits, authentication_failed,
# billing_error, connection_failed. No SubagentStop fires for such an agent (measured: start rows, no stop rows), so the
# reading below is the only way Big Dad learns it stopped and why.
QUIT_WHAT = {"limit": "the usage limit", "model-limit": "this model's usage limit", "login": "an expired login",
             "credits": "a low credit balance", "network": "a lost connection", "api-error": "an API error"}
QUIT_HELP = {
    "limit": "Nothing can run until the reset. Their work so far is kept in their logs (and worktrees): after the reset, "
             "resume each where it stopped (SendMessage to its agent id) instead of launching a fresh one that redoes it. "
             "Commit any half-done worktree changes as WIP first.",
    "model-limit": "Run /usage-credits to go on, or /model to pick another model; then resume each where it stopped "
                   "(SendMessage to its agent id) instead of a fresh launch.",
    "login": "Run /login, then resume each where it stopped (SendMessage to its agent id); their work so far is kept.",
    "credits": "Add API credits, or /login with a plan, then resume each where it stopped (SendMessage to its agent id).",
    "network": "When the connection is back, resume each where it stopped (SendMessage to its agent id).",
    "api-error": "Resume each where it stopped (SendMessage to its agent id); if it fails again, check status.claude.com.",
}


def quit_row(d):
    """A log row Claude Code wrote because a call failed -> {kind, limit, resets_at, text, uuid}; anything else -> None."""
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    api, err = dstr(d.get("apiError")), dstr(d.get("error"))
    if not (d.get("isApiErrorMessage") is True or (m.get("model") == "<synthetic>" and (api or err))):
        return None
    parts = m.get("content") if isinstance(m.get("content"), list) else []
    text = " ".join(dstr(p.get("text")) for p in parts if isinstance(p, dict)).strip()
    params = d.get("apiErrorParams") if isinstance(d.get("apiErrorParams"), dict) else {}
    info = params.get("rate_limit_info") if isinstance(params.get("rate_limit_info"), dict) else None
    info = info or (d.get("quotaLimits") if isinstance(d.get("quotaLimits"), dict) else {})
    if api == "usage_limit_reached":
        kind = "limit"
    elif api == "model_requires_usage_credits":
        kind = "model-limit"
    elif err == "authentication_failed":
        kind = "login"
    elif err == "billing_error":
        kind = "credits"
    elif api == "connection_failed" or err in ("server_error", "overloaded_error", "timeout"):
        kind = "network"
    else:
        kind = "api-error"
    rtype = dstr(info.get("rateLimitType"))
    limit = ""
    if kind == "limit":
        if rtype == "five_hour":
            limit = "5-hour"
        elif "week" in rtype or "seven_day" in rtype:
            limit = "weekly"
        else:
            g = re.search(r"hit your (.{1,20}?) limit", text)
            limit = g.group(1) if g else ""
    return {"kind": kind, "limit": limit, "resets_at": reset_at(info.get("resetsAt")), "text": text[:160],
            "uuid": dstr(d.get("uuid"))}


def reset_at(v):
    """A usable reset time (thirtieth review: the one rule quit_row, reset_label and quit_line share): an int or float,
    not a bool, finite, above 0; anything else -> None."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < float("inf") else None


def last_quit(path, n=32_000):
    """Why the log's last answer failed (quit_row of its last assistant row), or None if it ended normally."""
    for line in reversed(tail_text(path, n).splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(d, dict) and d.get("type") == "assistant":
            return quit_row(d)
    return None


def reset_label(at):
    """'2:50am', or 'Sun 2:50am' when the reset is not today (local time). A time the platform cannot convert (twenty-
    ninth review: 1.7e12 on Windows, 1e20, inf, nan, not a positive number) gives "", the no-reset-time label."""
    if reset_at(at) is None:                     # nan, inf, 0, a bool, not a number
        return ""
    try:
        t = time.localtime(at)
        s = f"{t.tm_hour % 12 or 12}{':%02d' % t.tm_min if t.tm_min else ''}{'am' if t.tm_hour < 12 else 'pm'}"
        return s if t[:3] == time.localtime()[:3] else time.strftime("%a ", t) + s
    except (OSError, OverflowError, ValueError):
        return ""


def quit_what(q):
    """'the 5-hour usage limit (resets 2:50am)', 'an expired login', ..."""
    what = QUIT_WHAT.get(q.get("kind"), QUIT_WHAT["api-error"])
    if q.get("kind") == "limit" and q.get("limit"):
        what = f"the {q['limit']} usage limit"
    r = reset_label(q.get("resets_at"))
    return what + (f" (resets {r})" if r else "")


def quit_line(agents, q):
    """One line for the main chat: who stopped, why, and what to do. agents = [(type, id), ...]."""
    names = ", ".join(f"{t or 'agent'} {short_id(a)}" for t, a in agents[:6]) + (f" +{len(agents) - 6}" if len(agents) > 6 else "")
    n = len(agents)
    help = QUIT_HELP.get(q.get("kind"), QUIT_HELP["api-error"])
    at = reset_at(q.get("resets_at"))
    if q.get("kind") == "limit" and at is not None and at <= time.time():
        help = ("The limit has reset: resume each where it stopped (SendMessage to its agent id) instead of launching "
                "a fresh one that redoes it; their work so far is kept in their logs (and worktrees).")
    return f"🟡 {n} helper{'s' if n != 1 else ''} stopped by {quit_what(q)}, not finished: {names}. " + help


# 3.0.0: every handoff ask names the same five things (the owner: "make sure these handoffs are complete").
# Shared by context_guard (the notes) and agent_stop (the hard stop's deny reason).
HANDOFF = "what's done, what's left, files changed, commit ids, tests run and their result"

# 3.0.0 fifteenth review: what reading one log row raises when a value is of the wrong kind (a list "id", a string
# "usage"...): that row is skipped whole. Sixteenth review: one copy for the five log readers (loop_guard,
# compact_slim, bd_models, bd_proof, bigdad cmd_modes).
ODD_ROW = (AttributeError, TypeError, KeyError, ValueError, IndexError)


def is_number(v):
    """A real number that is not a bool: the one test the status line and the receipts share (both call it here)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def entry_problem(st, uid=None):
    """'' when an lstat() result is safe to write into or move, else why not, without the path (the caller names
    it): a symlink, a junction or other reparse point, or (uid given, POSIX) another user's. One test for
    shrink._private and bd_tidy.ours, worded as check_state_dir words it."""
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink (it could point anywhere)"
    if getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return "is a junction or other reparse point (it could point anywhere)"
    if uid is not None and st.st_uid != uid:
        return f"belongs to another user (uid {st.st_uid})"
    return ""
