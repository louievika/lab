"""Big Dad tidy: clear Big Dad's own leftovers, never the user's.

What counts as a leftover (older than N days, default 7):
  - per-session state files the hooks write (bigdad-guard-, -reads-, -beat-, -reload-, -loop-, -watch-, -stop-, -bgwatch-,
    -outline-, -redirect-, -soft-, -agentscan-, -burn-, -pending-)
  - the same files, and the saved full outputs (bigdad-out, bigdad-out-<uid>), that 2.0.8 and earlier left in the
    system temp folder ("old temp"; only when BIGDAD_STATE_DIR is not set, since then 2.0.8 wrote there too). In that
    shared folder only entries that are ours are moved: never a link or junction, never another user's (the file's
    owner: POSIX uid, Windows owner SID; an owner that can't be read is left).
  - never a note a chat has not been shown yet (pending, or a redirect holding a dropped plan) while that chat's
    transcript is still there to resume, however old
  - Claude Code project folders from Big Dad's own headless test runs (probe / proof)
Never touched: the user's session logs, skills, plugins, settings, Big Dad's own logs
(agents/notes/receipts .jsonl: they are the evidence).

  tidy          show what would move (nothing changes)
  tidy yes      move it to ~/.claude/bigdad/trash/<date>/ with a manifest
  tidy undo     put the newest batch back where nothing has taken its place (never overwrites); old temp items
                stay in the trash: undo never writes into the shared system temp folder
  tidy empty    delete Big Dad's trash for good (only its own trash folder)
"""
import errno, json, os, re, shutil, stat, sys, time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
import bd_common as C

STATE = re.compile(r"^bigdad-(guard|reads|beat|reload|loop|watch|stop|bgwatch|outline|redirect|soft|agentscan|burn|pending|quit)-[\w.-]+$")
OWN_RUNS = re.compile(r"bigdad-(probe|proof)-[\w-]+$")
OLD_OUT = re.compile(r"^bigdad-out(-\d+)?$")       # 2.0.8's saved full outputs in the temp folder (shrink.out_dir)
PENDING = re.compile(r"^bigdad-pending-([A-Za-z0-9-]+)\.jsonl$")      # bd_common.pending_file
REDIRECT = re.compile(r"^bigdad-redirect-([\w.-]+)\.json$")           # bd_common.redirect_file


def days():
    try:
        return max(0.0, float(os.environ.get("BIGDAD_TIDY_DAYS", "7")))
    except ValueError:
        return 7.0


def size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def newest(p):
    """Newest file time under p (a folder in use has a fresh file somewhere inside)."""
    t = os.path.getmtime(p)
    if os.path.isdir(p):
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    t = max(t, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
    return t


WIN = os.name == "nt"
ADMINS = "S-1-5-32-544"                # BUILTIN\Administrators: the owner of what an elevated shell makes
_ME = []


def _sid_text(adv, k32, sid):
    import ctypes
    s = ctypes.c_void_p()
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(s)) or not s.value:
        return None
    try:
        return ctypes.wstring_at(s.value)
    finally:
        k32.LocalFree(s)


def _win_api():
    import ctypes
    from ctypes import wintypes
    adv, k32 = ctypes.WinDLL("advapi32"), ctypes.WinDLL("kernel32")
    k32.LocalFree.argtypes, k32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    return ctypes, wintypes, adv, k32


def _owner_sid(p):
    """Windows: the owner SID of p as text (S-1-5-...), or None when it can't be read. ctypes only, no subprocess."""
    try:
        ctypes, wintypes, adv, k32 = _win_api()
        f = adv.GetNamedSecurityInfoW
        f.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        f.restype = wintypes.DWORD
        owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
        if f(p, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(sd)) != 0:   # file object, owner only
            return None
        try:
            return _sid_text(adv, k32, owner) if owner.value else None
        finally:
            k32.LocalFree(sd)
    except (OSError, AttributeError, ValueError):
        return None


def _my_sid():
    """Windows: this process's user SID as text, or None. Read once."""
    if not _ME:
        try:
            ctypes, wintypes, adv, k32 = _win_api()
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD)]
            h, n = wintypes.HANDLE(), wintypes.DWORD()
            if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(h)):        # TOKEN_QUERY
                return None
            try:
                adv.GetTokenInformation(h, 1, None, 0, ctypes.byref(n))                          # TokenUser: size
                buf = ctypes.create_string_buffer(n.value or 1)
                if not adv.GetTokenInformation(h, 1, buf, n, ctypes.byref(n)):
                    return None
                sid = _sid_text(adv, k32, ctypes.c_void_p.from_buffer(buf).value)                # TOKEN_USER.User.Sid
            finally:
                k32.CloseHandle(h)
            if not sid:
                return None
            _ME.append(sid)
        except (OSError, AttributeError, ValueError):
            return None
    return _ME[0]


def ours(p):
    """An entry in the shared old temp folder Big Dad may move: not a link or junction, and owned by us (POSIX: the
    owner uid; Windows: the owner SID). An owner that can't be read is not ours. Windows, owned by Administrators
    (an elevated shell makes files owned by that group, not by you): ours only when the folder holding it is owned
    by us (your own per-user temp folder); in a shared folder it could be any admin's, so it is left."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if C.entry_problem(st, None if WIN else os.geteuid()):
        return False
    if not WIN:
        return True
    me, owner = _my_sid(), _owner_sid(p)
    if not me or not owner:
        return False
    return owner == me or (owner == ADMINS and _owner_sid(os.path.dirname(os.path.abspath(p))) == me)


def note_sid(n, p):
    """The chat a state file still holds a note for, or '': a pending note (a capped agent's relaunch line, shown and
    removed at that chat's next hook) or a redirect note holding a dropped plan (the next compact drops it, then it
    is cleared). Every other state file is counters and flags: losing an old one costs nothing."""
    m = PENDING.match(n)
    if m:
        return m.group(1)
    m = REDIRECT.match(n)
    return m.group(1) if m and isinstance(C.load_json(p).get("dropped"), dict) else ""


def sessions():
    """Ids of the chats Claude Code still keeps (projects/<folder>/<id>.jsonl): those can still be resumed."""
    proj, out = os.path.join(C.config_dir(), "projects"), set()
    try:
        folders = os.listdir(proj)
    except OSError:
        return out
    for d in folders:
        try:
            out.update(f[:-6] for f in os.listdir(os.path.join(proj, d)) if f.endswith(".jsonl"))
        except OSError:
            continue
    return out


def leftovers(fix=True):
    """fix=False (health): only look; a missing state folder is not made. A note its chat has not been shown yet
    (note_sid) is never a leftover by age alone: it stays while that chat's transcript is there to resume."""
    cut = time.time() - days() * 86400
    out, live = [], None
    sd = C.state_dir(fix)              # a refused state folder is never looked in (C.NO_STATE: nothing there)
    for n in sorted(os.listdir(sd)) if os.path.isdir(sd) else ():
        p = os.path.join(sd, n)
        if STATE.match(n) and os.path.isfile(p) and os.path.getmtime(p) < cut:
            sid = note_sid(n, p)
            if sid:
                live = sessions() if live is None else live
                if sid in live:
                    continue
            out.append(("state", p))
    old = C.legacy_state_dir()
    if old and os.path.isdir(old) and not C.same_file(old, C.state_path()):
        try:
            names = sorted(os.listdir(old))
        except OSError:
            names = []
        for n in names:
            p = os.path.join(old, n)
            if not (STATE.match(n) or OLD_OUT.match(n)) or not ours(p):
                continue
            try:
                if (os.path.isfile(p) and STATE.match(n) and os.path.getmtime(p) < cut) or (
                        os.path.isdir(p) and OLD_OUT.match(n) and newest(p) < cut):
                    out.append(("old temp", p))
            except OSError:
                continue
    proj = os.path.join(C.config_dir(), "projects")
    for n in sorted(os.listdir(proj)) if os.path.isdir(proj) else ():
        p = os.path.join(proj, n)
        if OWN_RUNS.search(n) and os.path.isdir(p) and newest(p) < cut:
            out.append(("test run", p))
    return out


def trash_root():
    return os.path.join(C.bd_dir(), "trash")


def kb(n):
    return f"{n / 1024:,.0f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:,.1f} MB"


def show(items):
    by = {}
    for kind, p in items:
        c, b = by.get(kind, (0, 0))
        by[kind] = (c + 1, b + size(p))
    for kind, (c, b) in by.items():
        print(f"  {c:>4}  {kind:<9} {kb(b)}")


def new_batch():
    """This run's own batch folder, made exclusively: a second `tidy yes` in the same second gets -01, -02... (sorts
    after it, so `tidy undo` still takes the newest)."""
    os.makedirs(trash_root(), exist_ok=True)
    base = os.path.join(trash_root(), time.strftime("%Y-%m-%d_%H%M%S"))
    for n in range(100):
        p = f"{base}-{n:02d}" if n else base
        try:
            os.mkdir(p)
            return p
        except FileExistsError:
            continue
    raise FileExistsError(base)


def _refused(act, head=True):
    """Fourteenth review: with the state folder refused Big Dad writes nothing in its own folder (another user may
    be able to write there), so undo, empty and yes move, delete and write nothing: True after saying so in one line.
    Fifteenth review: head=False (`tidy yes`, whose listing already named the reason) does not name it again."""
    why = C.shown(C.state_problem())    # thirty-seventh review: built from the state path (environment), safely
    if why:
        fix = f"`tidy {act}` works again once that folder is fixed (`bigdad.py health` shows it)."
        print(f"🥛 == TIDY {act}: nothing done: its state folder was refused: {why}. Big Dad writes nothing in its "
              f"own folder while it is; {fix}" if head else
              f"  nothing moved: Big Dad writes nothing in its own folder while that folder is refused; {fix}")
    return bool(why)


def cmd_tidy(rest):
    act = rest[0].lower() if rest else ""
    if act in ("undo", "empty") and _refused(act):
        return
    if act == "undo":
        return undo()
    if act == "empty":
        return empty()
    items = leftovers()
    print(f"🥛 == TIDY  Big Dad's own leftovers older than {days():g} days (never your logs, skills or settings)")
    if C.state_problem():
        print(f"  state folder not looked in, it was refused: {C.shown(C.state_problem())}")   # thirty-seventh review
    if not items:
        print("  nothing to tidy.")
        return
    show(items)
    if act != "yes":
        print("  nothing moved. `tidy yes` moves these to Big Dad's trash (reversible: `tidy undo`).")
        return
    if _refused(act, head=False):
        return
    batch = new_batch()
    moved = 0
    with open(os.path.join(batch, "manifest.jsonl"), "x", encoding="utf-8") as man:
        for i, (kind, p) in enumerate(items):
            dest = os.path.join(batch, f"{i:04d}-{os.path.basename(p)}")
            try:
                if os.path.lexists(dest):          # never overwrite a saved file
                    raise FileExistsError(dest)
                shutil.move(p, dest)
            except OSError as e:
                print(f"  skipped (in use?): {C.shown(os.path.basename(p))}: {e.__class__.__name__}")
                continue
            man.write(json.dumps({"kind": kind, "from": p, "to": dest}) + "\n")
            moved += 1
    print(f"  moved {moved} to {C.shown(batch)}. Undo: `tidy undo`. Delete for good: `tidy empty`.")


def batches():
    r = trash_root()
    return sorted(d for d in (os.listdir(r) if os.path.isdir(r) else ())
                  if os.path.isfile(os.path.join(r, d, "manifest.jsonl")))


NO_LINK = {errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def _copy_new(src, dst, folder):
    """Copy into a new name only (a folder: claimed with mkdir; a file: opened "xb"), then drop the trash copy.
    A copy that fails partway is removed, the trash copy stays. Once the copy is made, as move_new: True, or False
    when the trash copy could not be removed. Tenth review: when a folder copy fails and something is still at dst
    after that removal, ClaimLeft is raised (naming copytree's error), so undo marks the row and never removes it.
    Eleventh review: the file branch does the same when the partial file it made cannot be removed (the copy's
    error first, the removal's after it); both are ClaimLeft "partial". Only ever for a dst this call made: a name
    that was already there fails the exclusive create, before any copy. Twelfth review: in both branches anything
    raised once dst is made (the file's final flush on close, or a BaseException such as KeyboardInterrupt) goes
    through that one cleanup; a BaseException is raised again as itself, never as ClaimLeft."""
    if folder:
        os.mkdir(dst, 0o700)
        try:
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        except BaseException as e:
            shutil.rmtree(dst, ignore_errors=True)
            if isinstance(e, Exception) and os.path.lexists(dst):
                raise ClaimLeft(f"{e.__class__.__name__}, and the partial copy Big Dad made at that name could not "
                                f"be removed", "partial") from e
            raise
        shutil.rmtree(src, ignore_errors=True)
        return not os.path.lexists(src)
    with open(src, "rb") as a:
        b = open(dst, "xb")                # dst is Big Dad's own from here: the exclusive create made it
        try:
            shutil.copyfileobj(a, b)
            b.close()                      # its final flush may fail too (a full disk)
        except BaseException as e:
            try:
                b.close()                  # a close that failed has closed the file all the same
            except OSError:
                pass
            try:
                os.unlink(dst)
            except OSError as r:
                if isinstance(e, Exception):
                    raise ClaimLeft(f"{e.__class__.__name__}, and the partial copy Big Dad made at that name "
                                    f"could not be removed ({r.__class__.__name__})", "partial") from e
            raise
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass                               # the copy is made: its times are not worth calling it failed
    return _drop(src)


def _drop(src):
    """Remove the trash name once the item is back at its new name: True, or False when it could not be."""
    try:
        os.unlink(src)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


class ClaimLeft(OSError):
    """Ninth review: move_new made the folder that claims dst, the move onto it failed, and that folder could not be
    removed. What is at dst is Big Dad's own claim, so this is a failure (its words name the move's error and the
    removal's), never "the name is taken". It carries no errno, so nothing reads it as EEXIST or EXDEV.
    Eleventh review: kind says what was left at dst, "empty" (the claim folder) or "partial" (a partial copy)."""
    def __init__(self, words, kind):
        super().__init__(words)
        self.kind = kind


def move_new(src, dst):
    """Move src to dst and never over anything at dst: whatever appears there first wins and src stays (an
    OSError, FileExistsError when the name is taken). Windows: os.rename (it fails when dst exists). POSIX file:
    a hard link, then the trash name removed. POSIX folder: mkdir claims the name, rename moves onto the empty
    claim, the claim is removed if that fails. Across file systems (or no hard links): copied into a new name.
    POSIX symlink (second review): made again with os.symlink, which never goes over a name, so it comes back as a
    link, never as a hard link to its target; if that fails it stays in the trash (never copied through).
    True when moved; False (second review) when the item is back but the trash name could not be removed.
    Ninth review: when the claim cannot be removed after a failed rename, ClaimLeft is raised (the item stays in the
    trash; nothing else is removed), instead of the rename's error alone, which read as a name taken by someone else."""
    mode = os.lstat(src).st_mode
    folder, link = stat.S_ISDIR(mode), stat.S_ISLNK(mode)
    try:
        if WIN:
            os.rename(src, dst)
        elif folder:
            os.mkdir(dst, 0o700)
            try:
                os.rename(src, dst)
            except OSError as e:
                try:
                    os.rmdir(dst)
                except OSError as r:
                    raise ClaimLeft(f"{e.__class__.__name__}, and the folder Big Dad made at that name to claim it "
                                    f"could not be removed ({r.__class__.__name__})", "empty") from e
                raise
        elif link:
            os.symlink(os.readlink(src), dst)
        else:
            os.link(src, dst)              # a regular file here: symlinks took the branch above
        if WIN or folder:
            return True
    except OSError as e:
        if isinstance(e, ClaimLeft) or link or (e.errno != errno.EXDEV and (WIN or folder or e.errno not in NO_LINK)):
            raise
        return _copy_new(src, dst, folder)
    return _drop(src)                      # the new name exists: the item is back whatever the unlink does


def undo():
    """Put the newest batch back. Old temp items stay in the batch: putting them back would write into the shared
    system temp folder, where another user may have put something at that name since. Each other item goes back
    with move_new, so a name taken in the meantime keeps what is there; that item stays in the trash, named.
    3.0.0 second review: while such an item is left (name taken, or the move failed), the batch stays open, its
    manifest marking what was put back (third review), so `tidy undo` tries the rest again. Once only old temp
    items (never put back) or nothing is left, the batch is closed (manifest.restored.jsonl) and undo goes on to older ones.
    3.0.0 third review: undo walks the batches newest first. A run that restores something from a batch stops
    there. Sixth review: a batch from which this run restored nothing always goes on to the next older batch, whatever
    its rows are (taken, failed, held, or all found gone so the batch is closed): it says so in one line, naming
    what is left (or that the batch is closed), so no batch ever blocks the older ones. The oldest batch gives the full
    report instead. The only protection against restoring an older version is the hold rule: an older batch never restores a path
    any newer open batch still holds in the trash (taken, failed, held or odd alike); that row is left, named ("a newer
    copy is still in the trash"). Twenty-second review: exact paths only (_held_key, the folder resolved), never what
    lies inside a held folder."""
    b = batches()
    if not b:
        print("🥛 == TIDY undo: no batch to restore.")
        return
    held, trash = set(), _real_folder(trash_root())   # sixteenth review: the trash folder resolved once per run
    for i, name in enumerate(reversed(b)):
        if _undo_batch(name, i == len(b) - 1, held, trash):
            return


def _key(p):
    return os.path.normcase(os.path.abspath(p))


def _held_key(p):
    """p as the hold rule compares it (nineteenth review): the folder holding the name resolved, the name kept, so
    link/x and real/x are one place while a symlink is still judged where it is. Twentieth review: one rule for every
    input: made absolute first (a trailing separator, "." and ".." go), so link/x/ and link/x are one key too; _key
    only when resolving itself raises. Twenty-fourth review: never raises OSError or ValueError: when _key raises too
    (a relative path and os.getcwd failing, the current folder gone), the path as written, case-folded."""
    try:
        return _folder_real(os.path.abspath(p))
    except (OSError, ValueError):
        try:
            return _key(p)
        except (OSError, ValueError):
            return os.path.normcase(p)


def _path_state(path):
    """What is at path (a trash copy, or a marked row's original path), as a pair: ("gone", None) when it is absent
    (FileNotFoundError / NotADirectoryError), ("there", None) when it is there, ("unreadable", error name) when it
    could not be read: still there, never gone (eighth review: os.path.lexists says False on any OSError). Ninth
    review: the one place that decides it. Twelfth review: named _path_state (it was _trash_state). Fourteenth
    review: a path the os functions reject (ValueError) is "unreadable" too, never gone (_is_path keeps such rows out)."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "gone", None
    except (OSError, ValueError) as e:
        return "unreadable", e.__class__.__name__
    return "there", None


def _is_path(p):
    """A row's "from" or "to" that undo can use: a str with no NUL that neither os.fsencode nor os.lstat rejects
    with ValueError (fourteenth review: a path too long for Windows; a lone surrogate on POSIX)."""
    if not isinstance(p, str) or "\0" in p:
        return False
    try:
        os.fsencode(p)
        os.lstat(p)
    except ValueError:
        return False
    except OSError:
        pass
    return True


COPY = re.compile(r"[0-9]{4,}-.+", re.S)          # what `tidy yes` names a copy: f"{i:04d}-{name}" (never a manifest)
ROW_ERRORS = (KeyError, TypeError, AttributeError, ValueError, OSError)


def _real(p):
    """p as one comparable string: its folder's real path joined with its own name as it is (a symlink is judged by
    where it is, not where it points); case folded on Windows. A name "", "." or "..": ValueError."""
    if os.path.basename(p) in ("", ".", ".."):
        raise ValueError(p)
    return _folder_real(p)


def _folder_real(p):
    """Twenty-first review: the one copy of _real's and _held_key's rule: p's folder resolved, its name kept, case
    folded on Windows."""
    return os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p)))


def _real_folder(p):
    """The real path of folder p, case folded on Windows; None when it cannot be resolved (then nothing is in it)."""
    try:
        return os.path.normcase(os.path.realpath(p))
    except (OSError, ValueError):
        return None


def _in_batch(m, top):
    """Fourteenth review: True when row m's trash copy is in its own batch folder (top: that folder, from
    _real_folder, resolved once per batch). Fifteenth review: a direct child of it (never deeper: not a file inside a
    trashed folder), named as `tidy yes` names a copy (COPY), never a manifest. Any error, or top None: False.
    Sixteenth review: COPY alone keeps the manifests out (their names do not start with digits and a dash)."""
    try:
        to = _real(m["to"])
        return top is not None and os.path.dirname(to) == top and COPY.fullmatch(os.path.basename(to)) is not None
    except ROW_ERRORS:
        return False


def _from_trash(m, trash):
    """Fifteenth review: True when row m's "from" is in Big Dad's trash folder (trash: from _real_folder), or cannot
    be resolved: such a row is not a row (its "to" may be its only copy). Compared as _in_batch does."""
    try:
        p = _real(m["from"])
    except ROW_ERRORS:
        return True
    if trash is None:
        return True
    return p == trash or p.startswith(trash.rstrip(os.sep) + os.sep)


def _ours(m, top, trash):
    """A row undo may try (fifteenth review: one place): "from" and "to" are paths (_is_path), the copy is in this
    batch (_in_batch), "from" is not in the trash (_from_trash). m is a dict not marked restored or gone."""
    return all(_is_path(m.get(k)) for k in ("from", "to")) and _in_batch(m, top) and not _from_trash(m, trash)


def _linked_back(m):
    """Fourteenth review: an earlier run linked the item back (move_new's os.link), then stopped before the trash
    copy was dropped: the original path and the trash copy are one regular file (neither a link). Any error: False.
    Fifteenth review: never when "from" and "to" are the same name (that file is the only copy). Sixteenth review:
    the real protection is _from_trash (via _ours, checked first by _undo_batch: a "from" in the trash is never a
    row); this same-name check is only a second guard behind it, kept for a direct caller."""
    try:
        if _key(m["from"]) == _key(m["to"]):
            return False
        a, b = os.lstat(m["from"]), os.lstat(m["to"])
        return stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode) and os.path.samestat(a, b)
    except (OSError, ValueError):
        return False



def _not_a_folder(path):
    """The nearest existing ancestor of path (path included) when it is not a folder, else '' (seventh review)."""
    while not os.path.lexists(path):
        up = os.path.dirname(path)
        if up == path:
            return ""
        path = up
    return "" if os.path.isdir(path) else path


def _move_failed(e, m):
    """The outcome of row m whose parent folder or move raised e (see _restore_row)."""
    if isinstance(e, ClaimLeft):
        return e.kind, (m["from"], str(e))         # a failure whose row is marked with what was left (_mark)
    if isinstance(e, FileNotFoundError):
        state, err = _path_state(m["to"])          # emptied meanwhile (already back, or `tidy empty`)?
        if state == "gone":
            return "gone", None
        if state == "unreadable":                  # ninth review: the restore's own error first, then the re-check's
            return "failed", (m["from"], f"{e.__class__.__name__} (the trash copy could not be read: {err})")
    if e.errno in (errno.EEXIST, errno.ENOTEMPTY):
        return "taken", None
    return "failed", (m["from"], e.__class__.__name__)   # fifth review: a missing destination is a failure, retried


# Eleventh review: what a marked row says while something is still at its original path. Big Dad never removes it.
CLAIM_WHAT = {"empty": "an empty folder", "partial": "a partial copy", "other": "something"}
CLAIM_EMPTY = (f"{CLAIM_WHAT['empty']} Big Dad made at that name on an earlier try is still there: remove it by hand, "
               "then run `tidy undo` again")   # eighteenth review: what is left is spelled once, in CLAIM_WHAT
CLAIM_PARTIAL = (f"{CLAIM_WHAT['partial']} Big Dad made at that name on an earlier try is still there (it is not your "
                 "complete item, which is still in the trash): remove it by hand, then run `tidy undo` again")
CLAIM_OTHER = ("a leftover from an earlier try is at that name: check it, remove it by hand if it is not yours to "
               "keep, then run `tidy undo` again")
CLAIM_WORDS = {"empty": CLAIM_EMPTY, "partial": CLAIM_PARTIAL, "other": CLAIM_OTHER}
# Twelfth review: the trash copy is gone (often restored by hand), so what is at the path may be the user's own item.
LEFT_AT = {k: f"an earlier try had left {what} at that name and the trash copy is gone: check what is at {{}} now "
              f"(it may be your own item)" for k, what in CLAIM_WHAT.items()}
# Seventeenth review: a failed manifest save predicts nothing it cannot keep; a leftover this run marked is named,
# since its mark is lost with the save. Eighteenth review: an earlier run's mark survives the failed save and may
# name the other kind, so the line predicts only that the next undo may describe it wrongly.
SAVE_FAILED = ("the trash list could not be updated ({man}), so this run's results were not recorded{n}; the next "
               "`tidy undo` will look at these items again and may list a restored one as already gone from the trash "
               "or as a name that is taken; nothing was lost.")
CLAIM_LOST = ("{path}: Big Dad left {what} at that name in this run (your item is still in the trash): remove it by "
              "hand; the next `tidy undo` may not describe it correctly.")


def _mark(m):
    """What a row's mark says was left at its original path: "empty", "partial", "other" (twelfth review: any other
    value, true included; only an unreleased commit ever wrote true), or None (not marked)."""
    if "claim" not in m:
        return None
    return m["claim"] if m["claim"] in ("empty", "partial") else "other"


def _restore_row(m, held):
    """One manifest row of undo, tried once: exactly one (outcome, detail). m is never changed.
    "skip": not a row to try (not a row, or already marked restored or gone); kept as it was
    "temp": an old temp item still in the trash (there, or unreadable so still there): never put back
    "newer": a newer open batch still holds this exact path (its _held_key is in held; twenty-second review: never
            a folder it is in; an old temp row is never held: it is never put back)
    "gone": the trash copy is absent, checked before any restore attempt (seventh review), or found absent by a
            restore that raised FileNotFoundError
    "back": put back; "stuck_back": put back, but the trash copy could not be removed (detail: its trash path)
    "taken": the name is taken again (EEXIST / ENOTEMPTY)
    "failed": detail is (path, words): the error name, with what it is about when there is more to say
    "empty" / "partial": failed with ClaimLeft, the outcome being what was left at the name (the row is marked with
            it); detail is (path, words)
    "left": a marked row with something still at its original path (looked at, never changed): a failure, detail
            (path, words saying what is there), and the move is not tried
    "stale": a marked row with nothing at its original path: the caller drops the mark and tries the row again
    Eleventh review: Big Dad never removes anything at the original path on a later run. A marked row is looked at
    after the trash copy: gone, it is "gone" with detail the kind when something is still at the path (named once).
    Twelfth review: the look is _path_state, as for the trash copy; a stale mark is its own outcome.
    Fourteenth review: an unmarked row whose original path and trash copy are one regular file (an earlier run had
    linked it back, then stopped) is "back" (or "stuck_back"): only the trash copy is dropped.
    Sixteenth review: in undo, _undo_batch passes done rows and checks _ours first; the not-a-dict, restored/gone and
    _is_path checks here are a second guard, kept on purpose so a direct caller (the tests, any later one) is safe."""
    if not isinstance(m, dict) or m.get("restored") or m.get("gone"):
        return "skip", None
    if not all(_is_path(m.get(k)) for k in ("from", "to")):
        return "skip", None                        # not a row (fourth review; a NUL, thirteenth; _is_path, fourteenth)
    if m.get("kind") == "old temp":
        return ("gone", None) if _path_state(m["to"])[0] == "gone" else ("temp", None)
    if _held_key(m["from"]) in held:   # nineteenth review: _held_key; twenty-second review: that exact path only
        return "newer", None                       # fourth review: a newer batch still holds this path
    state, err = _path_state(m["to"])
    kind = _mark(m)
    if state == "gone":
        return "gone", (kind if kind and _path_state(m["from"])[0] != "gone" else None)
    if state == "unreadable":
        return "failed", (m["from"], err)          # eighth review: could not be read, so still there: retried
    if kind:
        look, err = _path_state(m["from"])         # a look only: nothing at the original path is ever changed
        if look == "gone":
            return "stale", None
        if look == "unreadable":
            return "failed", (m["from"], err)
        return "left", (m["from"], CLAIM_WORDS[kind])
    if _linked_back(m):                            # fourteenth review: already back; only the trash copy is dropped
        return ("back", None) if _drop(m["to"]) else ("stuck_back", m["to"])
    parent = os.path.dirname(os.path.abspath(m["from"]))   # twenty-second review: a relative "from" (tidy writes one
    try:                                                     # when CLAUDE_CONFIG_DIR is relative) is resolved as tidy
        os.makedirs(parent, exist_ok=True)                   # resolved it: from the current folder
    except (FileExistsError, NotADirectoryError, FileNotFoundError) as e:   # see _not_a_folder
        block = _not_a_folder(parent)
        return "failed", (m["from"], f"{e.__class__.__name__}, a parent folder is not a folder: {block}" if block
                          else e.__class__.__name__)
    except OSError as e:
        return _move_failed(e, m)
    try:
        moved = move_new(m["to"], m["from"])
    except OSError as e:
        return _move_failed(e, m)
    return ("back", None) if moved else ("stuck_back", m["to"])


def _save(man, lines):
    """Write a batch's manifest again (fourteenth review: bd_common.replace_file, which waits out a Windows share
    lock). Whatever stops the save, Ctrl+C included, the manifest stays as it was and manifest.jsonl.new is removed."""
    new = man + ".new"
    try:
        with open(new, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("".join(ln + "\n" for ln in lines))
        C.replace_file(new, man)
    except BaseException:
        try:
            os.unlink(new)
        except OSError:
            pass
        raise


_RESOLVE = object()     # _undo_batch called without the trash folder (a direct caller): it resolves it itself


def _undo_batch(name, oldest, held, trash=_RESOLVE):
    """One batch of undo (see undo). True when this run stops here; False when it restored nothing (whether it left
    some item or closed the batch), so undo goes on to the next older batch (the oldest batch gives its full report
    instead).
    held: the paths newer open batches still hold in the trash (a set of _held_key keys); this batch leaves those
    rows and, when undo goes on, adds every path it still holds (taken, failed, held or odd; not rows restored or
    gone). Twenty-second review: each key holds that exact path only, never what lies inside it.
    Third review: the manifest keeps every row, so the closed one still records the whole batch. A row put back is
    marked "restored": true and skipped by later runs; a line that is not a row is kept as it was. Fourth review:
    a row whose trash copy is already gone is marked "gone": true, reported once and skipped afterwards. Fifth
    review: only when the trash copy really is absent; a FileNotFoundError on the destination side is a failure.
    Seventh review: a row whose trash copy is absent is "gone" before any restore attempt, whatever the destination
    looks like; a parent folder that cannot be made names the nearest existing ancestor that is not a folder.
    Eighth review: "absent" is decided in one place (now _path_state); a trash copy that cannot be read is a failure
    (its error named, retried by a later run), and an old temp item that cannot be read stays as it is.
    Ninth review: each row's outcome comes from _restore_row; this loop only tallies it and rewrites the row.
    Tenth review: an outcome the tally does not know raises (KeyError) instead of dropping the row; a ClaimLeft row is
    a failure, marked with what was left, and that alone rewrites the manifest. Eleventh review: so does a stale
    mark dropped by _restore_row; outcomes that are only counted are counted, lists only for what is named by path.
    Twelfth review: a "stale" row has its mark dropped here and is tried again as an unmarked row (_restore_row
    never changes m); each named outcome's detail is its entry (a taken or newer row's is its path).
    Fourteenth review: a row whose trash copy is not inside this batch's folder (_in_batch) is not a row; the
    manifest is saved by _save.
    Fifteenth review: a row already marked restored or gone is passed first; a row that is not Big Dad's own (_ours:
    its copy is not a copy in this batch, its "from" is in the trash, or a path is unusable) is counted ("odd") and
    named in one line, and while there is one the batch stays open (its item may still be in the trash). A failed save
    of the rewritten manifest says first what it means for the items already put back.
    Seventeenth review: a failed save (any row changed) says this run's results were not recorded, how many were
    restored, that the next undo looks at them again, and names each leftover this run marked (the next undo will not
    know it is Big Dad's); it promises no outcome for the next run.
    Sixteenth review: an odd row's "from" (when _is_path) is held like a left row's, so an older batch never puts
    an older version back at that path; trash (Big Dad's trash folder, from _real_folder) comes from undo, resolved
    once per run (not given: resolved here)."""
    d = os.path.join(trash_root(), name)
    if trash is _RESOLVE:
        trash = _real_folder(trash_root())
    top, odd, odd_from = _real_folder(d), 0, []
    count = dict.fromkeys(("back", "gone", "temp"), 0)
    stuck, taken, newer, failed, leftover = [], [], [], [], []    # failed: (path, words)
    named = {"stuck_back": stuck, "taken": taken, "newer": newer, "failed": failed, "empty": failed,
             "partial": failed, "left": failed}
    changed, out, claimed = 0, [], []     # claimed: (path, "empty" / "partial") marked in this run
    man = os.path.join(d, "manifest.jsonl")
    with open(man, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().splitlines()
    try:
        for line in lines:
            out.append(line)
            try:
                m = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(m, dict) or m.get("restored") or m.get("gone"):
                continue                               # not a row, or done: kept as it was
            if not _ours(m, top, trash):               # fourteenth/fifteenth review: not Big Dad's own row
                odd += 1
                if _is_path(m.get("from")):            # sixteenth review: its path is held, as a left row's is
                    odd_from.append(m["from"])
                continue
            what, detail = _restore_row(m, held)       # ninth review: one outcome per row, tallied here
            stale = what == "stale"
            if stale:                                  # nothing at the path: the mark is dropped, the row tried unmarked
                m = {k: v for k, v in m.items() if k != "claim"}
                what, detail = _restore_row(m, held)
            if what == "skip":
                continue
            if what in count:
                count[what] += 1
            else:                                      # an unknown outcome raises KeyError here
                named[what].append(m["from"] if detail is None else detail)
            row = None
            if what in ("back", "stuck_back"):
                row = dict(m, restored=True)
            elif what == "gone":                       # not in the trash any more: marked, so reported once
                row = dict(m, gone=True)
                if detail:
                    leftover.append(LEFT_AT[detail].format(m["from"]))
            elif what in ("empty", "partial"):         # a ClaimLeft: marked with what was left at the name
                row = dict(m, claim=what)
                claimed.append((m["from"], what))      # seventeenth review: named if the save fails
            elif stale:                                # the mark dropped
                row = m
            if row is not None:
                out[-1] = json.dumps(row)
                changed += 1
    except BaseException:                          # thirteenth review: an exception or Ctrl+C still saves the marks
        if changed:
            try:
                _save(man, out + lines[len(out):])     # rows not reached: as they were
            except BaseException:
                pass                               # a failed save never masks what ended the loop
        raise
    else:
        if changed:
            try:
                _save(man, out)
            except BaseException:                  # fifteenth review: say what the failed save means, then fail
                n = count["back"] + len(stuck)     # seventeenth review: predict nothing; name this run's leftovers
                # thirty-sixth review: paths read from the manifest (a state file) printed safely, here and below
                print("  " + C.shown(SAVE_FAILED.format(man=man, n=f': {n} restored' if n else '')))
                for p, kind in claimed:
                    print("  " + C.shown(CLAIM_LOST.format(what=CLAIM_WHAT[kind], path=p)))
                raise
    back, kept, temp = count["back"] + len(stuck), count["gone"], count["temp"]
    left = taken + newer
    if not (left or failed or odd):
        os.replace(man, os.path.join(d, "manifest.restored.jsonl"))
    why = [f"{p}: {err}" for p, err in failed]
    table = (("the name is taken again (never overwritten)", taken), ("could not move back", why),
             ("a newer copy is still in the trash", newer))    # eighth review: one table for both reports
    alone = (f"{odd} row(s) in this batch could not be read as Big Dad's own and were left alone; the batch stays "
             f"open: {d}") if odd else ""
    if not (back or oldest):                       # nothing restored here (batch closed or open): one line, go on
        head = "nothing else to put back" if odd else "batch closed"
        held.update(map(_held_key, odd_from))  # sixteenth review: an odd row's path is held too (_held_key: 19th)
        if left or failed:
            held.update(map(_held_key, left + [p for p, _ in failed]))
            head = "left in the trash, " + "; ".join(f"{what}: {'; '.join(ps)}" for what, ps in table if ps)
        extra = ([f"{kept} already gone from the trash"] if kept else []) + leftover + (
            [f"{temp} old temp item(s) stay in {d} (never put back in the shared temp folder)"] if temp else []) + (
            [alone] if odd else [])
        print(C.shown(f"🥛 == TIDY undo: nothing restored from {name}; {head}." + "".join(f" {x}." for x in extra)
                      + " Trying the next older batch."))
        return False
    print(C.shown(f"🥛 == TIDY undo: restored {back} from {name}" + (f"; {kept} already gone from the trash." if kept else ".")))
    for x in leftover:
        print(f"  {C.shown(x)}.")
    for what, ps in table:
        for p in ps:
            print(f"  left in the trash, {what}: {C.shown(p)}")
    for p in stuck:
        print(f"  restored, but the trash copy could not be removed (`tidy empty` removes it): {C.shown(p)}")
    if temp:
        print(f"  {temp} old temp item(s) stay in {C.shown(d)}: undo never puts them back in the shared temp folder.")
    if left or failed:
        print(f"  {len(left) + len(failed)} item(s) left in the trash stay in this batch: `tidy undo` tries them again.")
    if odd:
        print(f"  {C.shown(alone)}")
    return True


def empty():
    r = trash_root()
    if not os.path.isdir(r):
        print("🥛 == TIDY empty: Big Dad's trash is already empty.")
        return
    n = size(r)
    shutil.rmtree(r, ignore_errors=True)
    print(f"🥛 == TIDY empty: deleted Big Dad's trash ({kb(n)}). Only Big Dad's own trash folder was touched.")
