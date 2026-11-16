"""Big Dad refresh: the startup upkeep, run in the background by the SessionStart hook (startup and /clear).

One at a time (bd_dir()/baseline.building, taken with O_EXCL; a lock older than 10 minutes is a crashed run).
  profile   rebuilt only when the setup fingerprint moved (Claude Code version, skills, plugins, MCP servers,
            hooks, window settings), when it is missing, or 7+ days old. Built without running any hook (the
            launch cost comes from Claude Code's logs; only `setup --measure`, typed by the user, runs hooks).
  baseline  updated from the per-log cache: only new or changed logs are read, rewritten only when a number
            moved. The full build (every log read again) stays the fallback when missing or 7+ days old.
  learning  ended once the baseline covers enough calls and a profile exists (bd_learn.end_if_ready).

  bigdad.py refresh [--cwd DIR] [--quiet]
"""
import json, os, sys, time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
import bd_common as C

LOCK_STALE = 600          # s: same rule as session_start.busy()


def lock_path():
    return os.path.join(C.bd_dir(), "baseline.building")


def take_lock():
    """True when this run now holds the lock (a stale one is taken over)."""
    os.makedirs(C.bd_dir(), exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path()) < LOCK_STALE:
                    return False
                os.remove(lock_path())
            except OSError:
                return False
        except OSError:
            return False
    return False


def drop_lock():
    try:
        os.remove(lock_path())
    except OSError:
        pass


def profile_why(cwd):
    """Why the profile needs a rebuild, or '' when it still matches this setup."""
    import bd_setup as S
    p = S.load()
    if not p:
        return "no profile yet"
    if not p.get("fingerprint"):
        return "no setup fingerprint yet"
    try:
        age = (time.time() - time.mktime(time.strptime(str(p.get("at", ""))[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (ValueError, TypeError, OverflowError, OSError):   # fortieth review: OSError too, as bd_setup.stale
        return "profile has no date"
    if age > S.STALE_DAYS:
        return f"profile is {age:.0f} days old"
    if p.get("fingerprint") != S.fingerprint(cwd):
        return "setup changed"
    return ""


def save_profile(p):
    """OSError when it can't (no .tmp left, the old profile whole): a folder in the way, or profile.json still open in
    another process (Windows) after bd_common.replace_file's wait (Held)."""
    import bd_setup as S
    os.makedirs(os.path.dirname(S.PROFILE), exist_ok=True)
    tmp = S.PROFILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=1)
    except OSError:
        C._drop(tmp)
        raise
    C.replace_file(tmp, S.PROFILE)


def not_saved(e):
    """What a step that could not save says: the file and why, never a traceback."""
    name = os.path.basename(getattr(e, "filename", None) or "") or "a file"
    if isinstance(e, C.Held):
        return f"not saved ({name} was open in another program for {e.waited:g} s; the next refresh tries again)"
    return f"not saved ({name.replace('.tmp', '')}: {getattr(e, 'strerror', None) or e}; the next refresh tries again)"


def baseline_step():
    import bd_baseline as B
    b = B.load()
    if B.stale(b):
        B.rebuild()
        return "built (full)"
    _, moved = B.update(b)
    return "updated" if moved else "unchanged"


def run(cwd=None, quiet=True):
    """One refresh. Returns a short status string (also printed unless quiet)."""
    cwd = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    if not take_lock():
        say = "🥛 Big Dad refresh: another refresh is running; nothing done."
        if not quiet:
            print(say)
        return say
    try:                               # each step on its own: a file another process holds open (Windows) or a
        import bd_setup as S           # folder in the way skips that step, never the others
        why = profile_why(cwd)
        prof = "profile current"
        if why:
            try:
                save_profile(S.build(cwd, measure=False))
                prof = f"profile built ({why})"
            except OSError as e:
                prof = "profile " + not_saved(e)
        try:
            base = "baseline " + baseline_step()
        except OSError as e:
            base = "baseline " + not_saved(e)
        try:
            import bd_learn
            if bd_learn.end_if_ready():
                base += " · learning over"
        except Exception:
            pass
    finally:
        drop_lock()
    say = f"🥛 Big Dad refresh: {prof} · {base}"
    if not quiet:
        print(say)
    return say


def cmd_refresh(rest):
    cwd = rest[rest.index("--cwd") + 1] if "--cwd" in rest and rest.index("--cwd") + 1 < len(rest) else None
    run(cwd, quiet="--quiet" in rest)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd_refresh(sys.argv[1:])
