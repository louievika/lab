"""Big Dad keep list (2.0.0): what the user chose on purpose. Big Dad cuts waste only.

A kept item still shows its cost, labelled "your choice". It is never ranked as a cut and never named in a nudge.
A proposal the user turns down is kept on its own (by "said no"), so it is never offered again.
`keep verbose` turns the trimming off (outline-first, output shrink).
Stored in ~/.claude/bigdad/keep.json (its own file: `setup` rewrites profile.json from scratch).

  bigdad.py keep <x> ["why"]      x = a skill, plugin, MCP server, setting or habit (kind:name also works)
  bigdad.py keep <x> --declined   what the model runs when you say no to a proposal
  bigdad.py keep --list | --remove <x> | verbose
"""
import json, os, re

import bd_common as C

KINDS = ("skill", "plugin", "mcp", "setting", "habit")
WHY_MAX = 200


def path():
    return os.path.join(C.bd_dir(), "keep.json")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def broken():
    """keep.json is there but unreadable (bad JSON, not a dict, a folder): a reason, else ''."""
    p = path()
    if os.path.isdir(p):
        return "keep.json is a folder, not a file"
    if not os.path.exists(p):
        return ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError, RecursionError):
        return "keep.json is not valid JSON"
    return "" if isinstance(d, dict) else "keep.json is not a JSON object"


def bad_name(x):
    """Why x can't be a keep entry, else ''."""
    kind, sep, name = str(x).strip().partition(":")
    if not norm(x):
        return "the name is empty"
    if sep and kind.lower() in KINDS and not name.strip():
        return f"'{x.strip()}' has no name after the colon"
    if ".." in x or "/" in x or "\\" in x:
        return "a name can't hold a path (/, \\ or ..)"
    return ""


def items():
    d = C.load_json(path())
    it = d.get("items") if isinstance(d.get("items"), dict) else {}
    return {k: v for k, v in it.items() if isinstance(k, str) and isinstance(v, dict) and norm(v.get("name") or k)}


def parse(x):
    """'mcp:vercel' -> ('mcp', 'vercel'); 'dataviz' -> ('any', 'dataviz')."""
    kind, sep, name = x.strip().partition(":")
    return (kind.lower(), name.strip()) if sep and kind.lower() in KINDS and name.strip() else ("any", x.strip())


def match(name, kind=None, kept=None):
    """The keep entry (its key) that covers this skill / plugin / MCP server / setting name, or None.
    A kept plugin covers its skills (plugin:skill); a short name covers a long server id (vercel -> claude_ai_Vercel)."""
    n = norm(name)
    if not n:
        return None
    parts = {norm(p) for p in re.split(r"[:@/]|__", str(name)) if p}
    for key, v in (kept if kept is not None else items()).items():
        if kind and v.get("kind") not in (None, "any", kind):
            continue
        k = norm(v.get("name") or key)
        if k and (k == n or k in parts or (len(k) >= 4 and n.endswith(k))):
            return key
    return None


def in_text(text, kept=None):
    """The keep entry named as a whole word in a finding's text (health), or None."""
    low = str(text).lower()
    for key, v in (kept if kept is not None else items()).items():
        name = str(v.get("name") or key).lower()
        if name and name != "verbose" and re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", low):
            return key
    return None


def verbose():
    """`keep verbose`: no trimming. Fails closed to normal (trimming on) when the file can't be read."""
    return "verbose" in items()


def save(it):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    why = broken()
    if why == "keep.json is a folder, not a file":
        raise OSError(why)
    if why:                              # never overwrite a file we can't read: keep a copy to fix by hand
        try:
            os.replace(p, p + ".bad")
        except OSError:
            pass
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "items": it}, f, indent=1, ensure_ascii=False)
        C.replace_file(tmp, p)           # hooks read keep.json: held open (Windows) is waited out
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def add(x, why="", by="you"):
    kind, name = parse(x)
    key = (f"{kind}:{name}" if kind != "any" else name).lower()
    with C.lock(path(), wait=1.0):
        it = items()
        old = it.get(key) or {}
        it[key] = {"kind": kind, "name": name, "why": why[:WHY_MAX] or old.get("why", ""), "by": by, "ts": C.now()}
        save(it)
    return key


def remove(x):
    kind, name = parse(x)
    with C.lock(path(), wait=1.0):
        it = items()
        hits = [k for k in it if k == x.strip().lower() or norm(it[k].get("name") or k) == norm(name)]
        for k in hits:
            it.pop(k)
        if hits:
            save(it)
    return hits


def cmd(rest):
    args = [a for a in rest if a != "--declined"]
    declined = "--declined" in rest
    bad = broken()
    if bad:
        print(f"🥛 Big Dad: {bad}, so the keep list reads as empty. Fix or delete {path()}"
              + (" (the next keep moves it to keep.json.bad first)." if "folder" not in bad else "."), file=os.sys.stderr)
    if declined and not args:
        print("🥛 Big Dad: --declined needs a name, nothing kept. Usage: bigdad.py keep <x> --declined", file=os.sys.stderr)
        raise SystemExit(2)
    if not args or args[0] == "--list":
        it = items()
        if not it:
            print("🥛 Big Dad keep list: empty. Keep something you chose on purpose: bigdad.py keep <x> [\"why\"]")
            return
        said_no = sum(1 for v in it.values() if v.get("by") == "said no")
        print(f"🥛 Big Dad keep list: {len(it)} kept (your choice)" + (f", {said_no} from a no" if said_no else ""))
        for key, v in sorted(it.items()):
            # thirty-sixth review: name, kind, by and why come from keep.json (a state file): printed safely
            why = f" · {C.shown(str(v['why']))}" if v.get("why") else ""
            print(f"  ⭐ {C.shown(str(v.get('name') or key))} ({C.shown(str(v.get('kind', 'any')))}, "
                  f"{C.shown(str(v.get('by', 'you')))}){why}")
        print("  Kept items still show their cost, never as a cut. Undo one: bigdad.py keep --remove <x>")
        return
    if args[0] == "--remove":
        if len(args) < 2:
            print("🥛 Big Dad: --remove needs a name, nothing changed. Usage: bigdad.py keep --remove <x>", file=os.sys.stderr)
            raise SystemExit(2)
        hits = remove(args[1])
        if hits and norm(args[1]) == "verbose":
            print("🥛 Big Dad keep verbose: off. Trimming is back on (outline-first, output shrink).")
        else:
            print(f"🥛 Big Dad: {args[1]} is off the keep list. It can show up as a proposal again." if hits
                  else f"🥛 Big Dad: {args[1]} wasn't on the keep list.")
        return
    x, why = args[0], " ".join(args[1:])
    no = bad_name(x)
    if no:
        print(f"🥛 Big Dad: not kept, {no}. Usage: bigdad.py keep <x> [\"why\"]", file=os.sys.stderr)
        raise SystemExit(2)
    try:
        add(x, why, "said no" if declined else "you")
    except OSError as e:
        print(f"🥛 Big Dad: couldn't save the keep list ({e}). Nothing changed.", file=os.sys.stderr)
        raise SystemExit(1)
    if x.lower() == "verbose":
        print("🥛 Big Dad keep verbose: on. No trimming (outline-first, output shrink). "
              "Undo: bigdad.py keep --remove verbose")
        return
    print(f"🥛 Big Dad: keeping {x.strip()} (your choice). It still shows its cost, never as a cut"
          + (", and won't be offered again." if declined else ".")
          + (f" Why cut to {WHY_MAX} characters." if len(why) > WHY_MAX else ""))
