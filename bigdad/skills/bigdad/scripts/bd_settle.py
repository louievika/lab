"""Big Dad settle: once an area stops moving, it is settled at that floor and Big Dad stops re-auditing it.

Each baseline rebuild with new chats in it adds one snapshot (startup load, tokens re-sent above the guide per
chat, share of chats past the guide). Two snapshots in a row within a few percent: that area is settled at that
floor. Settled areas are skipped in audits and only re-checked by the drift check: the background baseline then
runs every DRIFT_DAYS instead of every 7. A settled area that grows back past its floor is un-settled and the user
gets one line at the next session start (a systemMessage: 0 tokens for Claude). A lower reading moves the floor down.

  bigdad.py settle            one line per area: settled (floor, since), moving, or built back up
  bigdad.py settle reset      forget every floor (the next two snapshots settle again)
"""
import json, os, sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
import bd_common as C

KEEP = 12            # snapshots kept
DRIFT_DAYS = 21      # baseline refresh once every area is settled (7 while anything still moves)
FLAT = 0.05          # two readings within 5% (or within the floor below) = even
GROW = 0.10          # a settled area 10%+ over its floor (and past the floor below) = built back up
# area: (label, how to read it from a baseline, small absolute gap that never counts as a change)
AREAS = {
    "startup": ("startup load", lambda b: b.get("startup_median", 0), 2_000),
    "over_guide": ("re-sent above guide per chat", lambda b: b.get("above_guide", 0) / max(1, b.get("chats", 0)), 50_000),
    "past_guide": ("chats past the guide", lambda b: b.get("past_guide", 0) / max(1, b.get("chats", 0)), 0.10),
}


def path():
    return os.path.join(C.bd_dir(), "settle.json")


def load():
    s = C.load_json(path())
    s.setdefault("history", [])
    s.setdefault("settled", {})
    s.setdefault("grew", [])
    return s


def save(s):
    os.makedirs(C.bd_dir(), exist_ok=True)
    tmp = path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        C._drop(tmp)
        raise
    C.replace_file(tmp, path())         # the refresh reads settle.json: held open (Windows) is waited out


def _same(a, b, floor):
    return abs(a - b) <= max(floor, FLAT * max(abs(a), abs(b)))


def record(b):
    """Add one snapshot from a fresh baseline; settle, re-floor or un-settle each area. Returns the state.
    No new chats since the last snapshot: nothing is added (the same 20 chats would 'settle' anything)."""
    if not b or b.get("watching"):
        return None
    s = load()
    snap = {"ts": b.get("ts", C.now()), "newest": b.get("newest"), "logs": b.get("logs")}
    for k, (_, get, _) in AREAS.items():
        snap[k] = round(float(get(b)), 4)
    last = s["history"][-1] if s["history"] else None
    if last and (last.get("newest"), last.get("logs")) == (snap["newest"], snap["logs"]):
        return s
    s["history"] = (s["history"] + [snap])[-KEEP:]
    for k, (label, _, floor) in AREAS.items():
        v, st = snap[k], s["settled"].get(k)
        if st:
            st["checked"] = snap["ts"]
            if v > st["at"] + max(floor, GROW * st["at"]):
                s["grew"].append({"area": k, "from": st["at"], "to": v, "since": st["since"], "ts": snap["ts"],
                                  "shown": False})
                del s["settled"][k]
            elif v < st["at"] and not _same(v, st["at"], floor):
                st.update(at=v, since=snap["ts"])           # a lower floor: the new even point
        elif last and k in last and _same(v, last[k], floor):
            s["settled"][k] = {"at": round((v + last[k]) / 2, 4), "since": snap["ts"], "checked": snap["ts"]}
    s["grew"] = s["grew"][-KEEP:]
    save(s)
    return s


def all_settled(s=None):
    s = s if s is not None else load()
    return bool(s.get("settled")) and all(k in s["settled"] for k in AREAS)


def _fmt(k, v):
    return f"{v:.0%}" if k == "past_guide" else C.tok(v)


def unshown_line(s=None, mark=True):
    """The SessionStart line for areas that grew back (once each), or ''."""
    s = s if s is not None else load()
    new = [g for g in s.get("grew", []) if not g.get("shown")]
    if not new:
        return ""
    parts = [f"{AREAS[g['area']][0]} {_fmt(g['area'], g['from'])} -> {_fmt(g['area'], g['to'])}" for g in new
             if g.get("area") in AREAS]
    if mark:
        for g in new:
            g["shown"] = True
        save(s)
    return ("🥛 Big Dad: built back up since it settled: " + "; ".join(parts) +
            ". Look at what was added (`bigdad.py settle`, then `all`).") if parts else ""


def _next_snapshot():
    """Snapshots come from full baseline builds only (the in-between updates read just the new logs): say when the
    next one is due, so 0 snapshots next to a working baseline is not read as a fault."""
    try:
        import time
        import bd_baseline as B
        b = B.load()
        at = b.get("full_at") if b else None
        if b and not b.get("watching") and isinstance(at, (int, float)) and not isinstance(at, bool):
            due = time.strftime("%Y-%m-%d", time.localtime(at + B.refresh_days() * 86400))
            return f"snapshots come from full baseline builds only; the next is due {due}"
    except Exception:
        pass
    return "needs a baseline with new chats"


def lines(s):
    out = []
    n = sum(1 for k in AREAS if k in s["settled"])
    due = "every area settled: drift check every %d days" % DRIFT_DAYS if all_settled(s) else "still moving: weekly checks"
    out.append(f"🥛 Big Dad settle · {n} of {len(AREAS)} settled · {len(s['history'])} snapshots · {due}")
    last = s["history"][-1] if s["history"] else {}
    empty = not s["history"] and not s["settled"]
    if empty:
        out.append(f"  no data   all {len(AREAS)} areas: {_next_snapshot()}")
    for k, (label, _, _) in ([] if empty else AREAS.items()):
        st = s["settled"].get(k)
        if st:
            out.append(f"  settled   {label}: floor {_fmt(k, st['at'])} since {st['since'][:10]} "
                       f"(last check {st['checked'][:10]}): skip it in audits")
        elif k in last:
            prev = s["history"][-2][k] if len(s["history"]) > 1 and k in s["history"][-2] else None
            was = f"{_fmt(k, prev)} -> " if prev is not None else ""
            out.append(f"  moving    {label}: {was}{_fmt(k, last[k])}: audit and cut here")
        else:
            out.append(f"  no data   {label}: {_next_snapshot()}")
    for g in s.get("grew", [])[-3:]:
        if g.get("area") in AREAS:
            out.append(f"  grew back {AREAS[g['area']][0]}: {_fmt(g['area'], g['from'])} -> {_fmt(g['area'], g['to'])} "
                       f"({g['ts'][:10]})")
    return out


def cmd_settle(rest):
    if rest[:1] == ["reset"]:
        s = load()
        s["settled"], s["grew"] = {}, []
        save(s)
        print("🥛 Big Dad settle: floors forgotten; the next two snapshots with new chats settle again.")
        return
    print("\n".join(lines(load())))


if __name__ == "__main__":
    cmd_settle(sys.argv[1:])
