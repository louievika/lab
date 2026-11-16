"""Big Dad learning period (2.0.6): on an install with no usable baseline, Big Dad watches before it acts.

While learning, no hook blocks, cuts or notes anything; each one only counts what it saw (bd_common.hold ->
learn-seen.jsonl, one {"k": kind} row each). Opt-ins the user turned on himself still act: the hard stop, the
pause, `bigdad.py watch`, the `receipt` prompt.

It ends by data, not by a calendar: a baseline covering at least BIGDAD_LEARN_CALLS API calls (default 30) and a
profile. With past logs that is the first background refresh; for a new user, the refresh the hooks start while
learning (at most once per RECHECK seconds). Then one line says so; learn.json keeps what was seen.

Files in bd_dir(): learning (the flag the hooks stat), learned (the one-time line), learn-seen.jsonl, learn.json.
Decided once per install: a baseline from before 2.0.6 means learning never starts.

  BIGDAD_LEARN=off          act at once (no learning)
  BIGDAD_LEARN_CALLS=N      calls the baseline must cover before learning ends (default 30)
"""
import json, os, sys, time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
import bd_common as C

MIN_CALLS = 30
RECHECK = 120             # s: while learning, the hooks start a background refresh at most this often

KINDS = (("second_read", "second read", "second reads"), ("big_read", "big whole-file read", "big whole-file reads"),
         ("big_output", "large output", "large outputs"),
         ("bg_reread", "background output read again", "background outputs read again"),
         ("over_guide", "tool call past the guide", "tool calls past the guide"), ("compact", "compact", "compacts"))


def min_calls():
    try:
        n = int(os.environ.get("BIGDAD_LEARN_CALLS") or MIN_CALLS)
        return n if n > 0 else MIN_CALLS
    except ValueError:
        return MIN_CALLS


def _p(name):
    return os.path.join(C.bd_dir(), name)


def state():
    d = C.load_json(_p("learn.json"))
    return d if isinstance(d, dict) else {}


def _save(d):
    tmp = _p("learn.json.tmp")
    try:
        os.makedirs(C.bd_dir(), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        C.replace_file(tmp, _p("learn.json"))   # held open elsewhere (Windows): the old state stays
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def decide(b):
    """begin()'s decision, read only: None (BIGDAD_LEARN=off, or already decided in learn.json), "on" (a usable
    baseline already there) or "learning"."""
    if C.off("BIGDAD_LEARN") or state().get("state"):
        return None
    if b and not b.get("watching"):
        return "on"
    return "learning"


def can_begin(source="startup", fix=True):
    """session_start's gates before begin(), in its order: BIGDAD_GUARD on, a usable state folder (C.state_problem:
    session_start returns early without one), a startup or /clear start, BIGDAD_BASELINE not off, BIGDAD_REFRESH and
    BIGDAD_LEARN on. session_start reads its learning switch here, and would_begin() too, so they cannot drift (3.0.1,
    second review: would_begin skipped the state-folder and source gates). fix=False only looks."""
    return (not C.off("BIGDAD_GUARD") and not C.state_problem(fix) and source in ("startup", "clear")
            and os.environ.get("BIGDAD_BASELINE") != "off" and not C.off("BIGDAD_REFRESH") and not C.off("BIGDAD_LEARN"))


def would_begin():
    """True when the next normal session start (a startup) would start learning: can_begin("startup") without
    making anything, then decide() on the stored baseline. Writes nothing; any error: False."""
    try:
        if not can_begin("startup", fix=False):
            return False
        import bd_baseline as B
        return decide(B.load()) == "learning"
    except Exception:
        return False


def begin(b):
    """Session start: decide once. A usable baseline already there (an update from 2.0.5): on at once."""
    d = decide(b)
    if d is None:
        return
    if d == "on":
        _save({"state": "on", "at": C.now(), "why": "baseline already there"})
        return
    _save({"state": "learning", "since": C.now()})
    try:
        open(_p("learning"), "w").close()
    except OSError:
        pass


def tally():
    seen = {}
    try:
        with C.open_wait(_p("learn-seen.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    k = json.loads(line).get("k")
                except (ValueError, AttributeError, RecursionError):
                    continue
                if isinstance(k, str):
                    seen[k] = seen.get(k, 0) + 1
    except OSError:
        pass
    return seen


def plural(n, one, many=None):
    return f"{n:,} {one if n == 1 else many or one + 's'}"


def seen_text(seen):
    parts = [plural(seen[k], one, many) for k, one, many in KINDS if seen.get(k)]
    return " · ".join(parts)


def line(b, seen):
    # thirty-second review: every number here goes through C.sane, so a damaged baseline.json cannot raise in the
    # formatting (a str chats once did, after the learning flag was gone, and the one "it is on" line was lost)
    text = (f"🥛 Big Dad has its baseline: {plural(C.sane(b.get('chats'), whole=True), 'chat')}, "
            f"{C.sane(b.get('calls'), whole=True):,} calls counted. It is on now.")
    s = seen_text(seen)
    if s:
        text += f" While learning it only watched (observed, nothing changed): {s}."
    return text


def end_if_ready():
    """Learning over when the baseline covers min_calls() and a profile exists: the line, else None."""
    if not os.path.exists(_p("learning")):
        return None
    import bd_baseline as B
    import bd_setup as S
    b = B.load()
    if not isinstance(b, dict) or b.get("watching"):
        return None
    calls = b.get("calls")             # past NUM_TOP, a bool or a float: not ready (32nd review)
    if not C.sane_ok(calls, whole=True) or calls < min_calls() or not os.path.exists(S.PROFILE):
        return None

    def built():
        seen = tally()
        st = state()
        st.update({"state": "on", "ended": C.now(), "chats": C.sane(b.get("chats"), whole=True), "calls": calls,
                   "seen": seen})
        return line(b, seen), st

    text, st = built()                 # built before the flag goes: proves they can be, so nothing after the
    try:                               # removal can raise and lose the one "it is on" line
        C.remove_file(_p("learning"))  # a flag held open elsewhere (Windows) means try again next refresh,
    except OSError:                    # not a second "it is on" line (and a refresh racing us gets here second)
        return None
    try:                               # thirty-third review: counted again now the flag is gone, so rows other
        text, st = built()             # sessions' hooks appended since the first count are in the line (the file
    except Exception:                  # is deleted below); if that fails, the first build stands
        pass
    # thirty-fourth review: still open, a short window: a hook that checked C.learning() before the removal and
    # appends its row after this recount is not in the line (the file goes below). Closing it needs a lock.
    try:
        with open(_p("learned"), "w", encoding="utf-8") as f:
            f.write(text)              # the line first, then learn.json; each survives the other failing
    except OSError:
        pass
    _save(st)
    try:
        C.remove_file(_p("learn-seen.jsonl"))
    except OSError:
        pass                           # counted again next time: the line and learn.json already hold the numbers
    return text


def tick(cwd):
    """A hook while learning: at most once per RECHECK s, a refresh (background; in-process with
    BIGDAD_BASELINE_SYNC=1), which ends learning when the data is in."""
    flag = _p("learning")
    try:
        if time.time() - os.path.getmtime(flag) < RECHECK:
            return
        os.utime(flag, None)
    except OSError:
        return
    if os.environ.get("BIGDAD_BASELINE_SYNC") == "1":
        import bd_refresh
        bd_refresh.run(cwd)
        return
    import session_start
    session_start.refresh_in_background(cwd or os.getcwd())
