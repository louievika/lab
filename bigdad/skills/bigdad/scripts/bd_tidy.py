"""Big Dad tidy: clear Big Dad's own leftovers, never the user's.

What counts as a leftover (older than N days, default 7):
  - per-session state files the hooks write (bigdad-guard-, -reads-, -beat-, -reload-, -loop-, -watch-, -stop-, -bgwatch-,
    -outline-, -redirect-, -soft-, -agentscan-, -burn-, -pending-)
  - Claude Code project folders from Big Dad's own headless test runs (probe / proof)
Never touched: the user's session logs, skills, plugins, settings, Big Dad's own logs
(agents/notes/receipts .jsonl: they are the evidence).

  tidy          show what would move (nothing changes)
  tidy yes      move it to ~/.claude/bigdad/trash/<date>/ with a manifest
  tidy undo     put the newest batch back
  tidy empty    delete Big Dad's trash for good (only its own trash folder)
"""
import json, os, re, shutil, sys, time

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
import bd_common as C

STATE = re.compile(r"^bigdad-(guard|reads|beat|reload|loop|watch|stop|bgwatch|outline|redirect|soft|agentscan|burn|pending)-[\w.-]+$")
OWN_RUNS = re.compile(r"bigdad-(probe|proof)-[\w-]+$")


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


def leftovers():
    cut = time.time() - days() * 86400
    out = []
    sd = C.state_dir()
    for n in sorted(os.listdir(sd)) if os.path.isdir(sd) else ():
        p = os.path.join(sd, n)
        if STATE.match(n) and os.path.isfile(p) and os.path.getmtime(p) < cut:
            out.append(("state", p))
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


def cmd_tidy(rest):
    act = rest[0].lower() if rest else ""
    if act == "undo":
        return undo()
    if act == "empty":
        return empty()
    items = leftovers()
    print(f"🥛 == TIDY  Big Dad's own leftovers older than {days():g} days (never your logs, skills or settings)")
    if not items:
        print("  nothing to tidy.")
        return
    show(items)
    if act != "yes":
        print("  nothing moved. `tidy yes` moves these to Big Dad's trash (reversible: `tidy undo`).")
        return
    batch = os.path.join(trash_root(), time.strftime("%Y-%m-%d_%H%M%S"))
    os.makedirs(batch, exist_ok=True)
    moved = 0
    with open(os.path.join(batch, "manifest.jsonl"), "a", encoding="utf-8") as man:
        for i, (kind, p) in enumerate(items):
            dest = os.path.join(batch, f"{i:04d}-{os.path.basename(p)}")
            try:
                shutil.move(p, dest)
            except OSError as e:
                print(f"  skipped (in use?): {os.path.basename(p)}: {e.__class__.__name__}")
                continue
            man.write(json.dumps({"kind": kind, "from": p, "to": dest}) + "\n")
            moved += 1
    print(f"  moved {moved} to {batch}. Undo: `tidy undo`. Delete for good: `tidy empty`.")


def batches():
    r = trash_root()
    return sorted(d for d in (os.listdir(r) if os.path.isdir(r) else ())
                  if os.path.isfile(os.path.join(r, d, "manifest.jsonl")))


def undo():
    b = batches()
    if not b:
        print("🥛 == TIDY undo: no batch to restore.")
        return
    d = os.path.join(trash_root(), b[-1])
    back = kept = 0
    with open(os.path.join(d, "manifest.jsonl"), encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if os.path.exists(m["to"]) and not os.path.exists(m["from"]):
            os.makedirs(os.path.dirname(m["from"]), exist_ok=True)
            shutil.move(m["to"], m["from"])
            back += 1
        else:
            kept += 1
    os.replace(os.path.join(d, "manifest.jsonl"), os.path.join(d, "manifest.restored.jsonl"))
    print(f"🥛 == TIDY undo: restored {back} from {b[-1]}" + (f"; {kept} left (already back or path taken)." if kept else "."))


def empty():
    r = trash_root()
    if not os.path.isdir(r):
        print("🥛 == TIDY empty: Big Dad's trash is already empty.")
        return
    n = size(r)
    shutil.rmtree(r, ignore_errors=True)
    print(f"🥛 == TIDY empty: deleted Big Dad's trash ({kb(n)}). Only Big Dad's own trash folder was touched.")
