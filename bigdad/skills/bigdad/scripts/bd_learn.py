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


def begin(b):
    """Session start: decide once. A usable baseline already there (an update from 2.0.5): on at once."""
    if C.off("BIGDAD_LEARN") or state().get("state"):
        return
    if b and not b.get("watching"):
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
                except (ValueError, AttributeError):
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
    text = (f"🥛 Big Dad has its baseline: {plural(b.get('chats', 0), 'chat')}, {b.get('calls', 0):,} calls counted. "
            "It is on now.")
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
    if not b or b.get("watching") or not isinstance(b.get("calls"), int) or b["calls"] < min_calls() \
            or not os.path.exists(S.PROFILE):
        return None
    try:
        C.remove_file(_p("learning"))  # first: a flag held open elsewhere (Windows) means try again next refresh,
    except OSError:                    # not a second "it is on" line (and a refresh racing us gets here second)
        return None
    seen = tally()
    text = line(b, seen)
    st = state()
    st.update({"state": "on", "ended": C.now(), "chats": b.get("chats", 0), "calls": b["calls"], "seen": seen})
    _save(st)
    try:
        with open(_p("learned"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
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
