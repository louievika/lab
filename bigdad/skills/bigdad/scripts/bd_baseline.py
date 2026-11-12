"""Big Dad baseline: the starting point every later receipt is measured against.

Built once from the logs Claude Code already keeps (the newest 20 main chats), so a new install has a baseline on
day one. With no logs yet, Big Dad watches first and builds it later. Saved to ~/.claude/bigdad/baseline.json.

  bigdad.py baseline            print the baseline receipt (builds it if missing or 7+ days old; 21 once settled)
  bigdad.py baseline --save     rebuild now, every log read again

Between full builds, the startup refresh calls update(): a per-log cache (baseline-cache.json, keyed by path, size and
mtime, holding each log's call sizes) means only new or changed logs are read; nothing moved, nothing is written.
The full build stays the fallback every 7 days (full_at), 21 once every area is settled.
"""
import glob, json, os, statistics, sys, time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
import bd_common as C

NEWEST = 20
STALE_DAYS = 7


def path():
    return os.path.join(C.bd_dir(), "baseline.json")


def logs():
    return sorted(glob.glob(os.path.join(C.config_dir(), "projects", "*", "*.jsonl")), key=os.path.getmtime)


def calls(p):
    """[(tokens sent, of which re-read from cache)] per API call, one per message id."""
    out, last = [], None
    try:
        f = C.open_wait(p, "rb")       # a command waits out a log held open elsewhere (Windows)
    except OSError:
        return out
    with f:
        for raw in f:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            m = d.get("message") if isinstance(d, dict) else None
            u = m.get("usage") if isinstance(m, dict) else None
            if d.get("type") != "assistant" or not isinstance(u, dict):
                continue
            mid = m.get("id") or d.get("requestId")
            if mid and mid == last:
                continue                       # Claude Code writes one row per content block of the same call
            last = mid
            try:
                n = [int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
            except (TypeError, ValueError):
                continue
            out.append((sum(n), n[2]))
    return out


def cache_path():
    return os.path.join(C.bd_dir(), "baseline-cache.json")


def load_cache():
    d = C.load_json(cache_path())
    logs_ = d.get("logs") if isinstance(d, dict) else None
    return logs_ if isinstance(logs_, dict) else {}


def save_cache(cache):
    tmp = cache_path() + ".tmp"
    try:
        os.makedirs(C.bd_dir(), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"logs": cache}, f)
        C.replace_file(tmp, cache_path())       # held open elsewhere (Windows): the old cache stays; logs read again
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _cached(hit, stamp):
    """The call list a cache entry holds when it still matches the log's size + mtime, else None."""
    if not isinstance(hit, dict) or hit.get("stamp") != stamp or not isinstance(hit.get("calls"), list):
        return None
    out = []
    for t in hit["calls"]:
        if not (isinstance(t, list) and len(t) == 2 and all(type(x) is int for x in t)):
            return None
        out.append((t[0], t[1]))
    return out


def scan(paths, cache):
    """(call lists per log, fresh cache for exactly these logs). Only logs not in the cache with the same size and
    mtime are read."""
    out, fresh = [], {}
    for p in paths:
        try:
            st = os.stat(p)
            stamp = [st.st_size, st.st_mtime_ns]
        except OSError:
            stamp = None
        c = _cached(cache.get(p), stamp) if stamp else None
        if c is None:
            c = calls(p)
        if stamp:
            fresh[p] = {"stamp": stamp, "calls": [list(t) for t in c]}
        out.append(c)
    return out, fresh


def build(cache=None):
    """The baseline from the newest NEWEST logs. cache=None reads every one of them (a full build); either way the
    per-log cache is rewritten for those logs."""
    every = logs()
    guide = C.guard_at()
    per_log, fresh = scan(every[-NEWEST:], cache or {})
    if cache is None or fresh != cache:
        save_cache(fresh)
    chats = [c for c in per_log if c]
    b = {"ts": C.now(), "guide": guide, "chats": len(chats), "logs": len(every), "shown": False}
    if every:
        b["newest"] = int(os.path.getmtime(every[-1]))     # settle adds a snapshot only when this moves
        b["days"] = round((os.path.getmtime(every[-1]) - os.path.getmtime(every[0])) / 86400, 1)
    if not chats:
        b["watching"] = True                   # nothing to measure yet: watch first
        return b
    flat = [t for c in chats for t in c]
    sent = sum(t for t, _ in flat)
    peaks = [max(t for t, _ in c) for c in chats]
    b.update({
        "calls": len(flat),
        "sent": sent,
        "resent_share": round(sum(r for _, r in flat) / sent, 4) if sent else 0,
        "startup_median": int(statistics.median(c[0][0] for c in chats)),
        "peak": max(peaks),
        "past_guide": sum(1 for x in peaks if x >= guide),
        "above_guide": sum(max(0, t - guide) for t, _ in flat),
    })
    return b


def save(b):
    """OSError when it can't (no .tmp left, the old file whole): a folder in the way, or baseline.json still open in
    another process (Windows) after bd_common.replace_file's wait (Held). Hooks catch it; commands say so."""
    os.makedirs(C.bd_dir(), exist_ok=True)
    tmp = path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(b, f, indent=1)
    except OSError:
        C._drop(tmp)
        raise
    C.replace_file(tmp, path())


def load():
    try:
        with C.open_wait(path(), encoding="utf-8", errors="replace") as f:
            b = json.load(f)
        return b if isinstance(b, dict) else None
    except (OSError, ValueError):
        return None


def refresh_days():
    """7 while anything still moves; once every area is settled, only the drift check (bd_settle.DRIFT_DAYS)."""
    try:
        import bd_settle
        return bd_settle.DRIFT_DAYS if bd_settle.all_settled() else STALE_DAYS
    except Exception:
        return STALE_DAYS


SAME_SKIP = ("ts", "shown", "full_at")


def update(old):
    """(baseline, moved): the baseline again from the per-log cache, reading only new or changed logs. Nothing moved:
    the old baseline as it was, nothing written. Moved: saved, keeping `shown` and the full-build clock (full_at;
    a 2.0.5 baseline has none, so its file time is the clock). Settle is not told: it snapshots full builds only."""
    new = build(load_cache())
    if old and {k: v for k, v in new.items() if k not in SAME_SKIP} == {k: v for k, v in old.items() if k not in SAME_SKIP}:
        return old, False
    full_at = old.get("full_at") if old else None
    if not isinstance(full_at, (int, float)):
        try:
            full_at = os.path.getmtime(path())
        except OSError:
            full_at = time.time()
    new["full_at"] = full_at
    if old and not old.get("watching"):
        new["shown"] = bool(old.get("shown"))
    save(new)
    return new, True


def rebuild():
    """Full build (every log read again), save, and hand the new numbers to settle (a settle error never loses
    the baseline)."""
    b = build()
    b["full_at"] = time.time()
    save(b)
    try:
        import bd_settle
        bd_settle.record(b)
    except Exception:
        pass
    return b


def stale(b):
    """True when a full build is due: no baseline, or 7+ days (21 once settled) since the last full build."""
    if not b:
        return True
    try:
        at = b.get("full_at")
        at = at if isinstance(at, (int, float)) and not isinstance(at, bool) else os.path.getmtime(path())
        return time.time() - at > refresh_days() * 86400
    except OSError:
        return True


def big(n):
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def one_line(b):
    """The baseline as one status line (SessionStart shows it once)."""
    if b.get("watching"):
        return "🥛 Big Dad is on and watching first: no chats logged yet, so your baseline comes after your first sessions."
    return (f"🥛 Big Dad baseline 🧾 {b['chats']} chats · {b['resent_share']:.0%} of input re-sent · startup "
            f"{C.money(b['startup_median'])} · {b['past_guide']} went past the {b['guide'] // 1000}k guide (peak "
            f"{C.money(b['peak'])}/call) · {big(b['above_guide'])} re-sent above it: that's what Big Dad goes after. "
            "Details: bigdad.py baseline")


def learning_rows(b, d, e):
    """While the `learning` flag exists: calls the baseline covers against the minimum, and what the hooks counted."""
    try:
        import bd_learn
        if not os.path.exists(bd_learn._p("learning")):
            return []
        need, seen = bd_learn.min_calls(), bd_learn.seen_text(bd_learn.tally())
    except Exception:
        return []
    have = b.get("calls") if isinstance(b.get("calls"), int) and not b.get("watching") else 0
    return [f"  👀 learning first    {have:,} of {need:,}   {d}calls; it only watches until {need:,}{e}",
            f"  👀 counted so far    {d}observed, nothing changed: {seen or 'nothing counted yet'}{e}"]


def receipt(b, color):
    g, r, y, d, e = ("\033[32m", "\033[1;31m", "\033[33m", "\033[2m", "\033[0m") if color else ("",) * 5
    w = 46
    rows = [f"🥛 BIG DAD · BASELINE RECEIPT", d + b.get("ts", "")[:10] + e, "─" * w]
    learning = learning_rows(b, d, e)
    if b.get("watching"):
        rows += ["  watching first: no chats logged yet.", "  Big Dad builds your baseline after your first sessions."]
        rows += learning
    else:
        span = f" over {b['days']:g} days" if b.get("days") is not None else ""
        rows += [
            f"  chats read            {b['chats']:>6}   {d}newest {NEWEST} of {b['logs']}{span}{e}",
            f"  💲 input sent         {big(b['sent']):>6}   {d}{b['calls']:,} calls{e}",
            f"  🔁 re-sent context    {b['resent_share']:>6.1%}   {d}of all input, read back from cache{e}",
            f"  🚀 startup load       {C.money(b['startup_median']):>6}   {d}first call, median{e}",
            f"  {'🔴' if b['past_guide'] else '🟢'} past the guide       {b['past_guide']:>3} of {b['chats']}   {d}peak {C.money(b['peak'])}/call{e}",
            f"  🧾 {r if b['above_guide'] else g}re-sent above guide  {big(b['above_guide']):>6}{e}   {d}what Big Dad goes after{e}",
        ]
        rows += learning
        try:                                   # 2.0.6: what the hooks only counted while Big Dad learned first
            import bd_learn
            seen = bd_learn.seen_text(bd_learn.state().get("seen") or {})
        except Exception:
            seen = ""
        if seen and not learning:
            rows.append(f"  👀 while learning     {d}observed, nothing changed: {seen}{e}")
    rows += ["─" * w, f"  {g}next:{e} /compact at a break · `bigdad.py all` for detail"]
    return "\n".join(rows)


def cmd_baseline(rest):
    b = load()
    if "--save" in rest or stale(b):
        b = rebuild()
    if "--quiet" in rest:
        return
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    print(receipt(b, color))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd_baseline(sys.argv[1:])
