"""`bigdad.py sentinel on|off|check` and `bigdad.py statusline on|off [--replace]`: opt-in, one setting each.

A plugin that does not load can't warn anyone, so the sentinel lives outside the plugin folder: copies of
hooks/sentinel.py and hooks/run.sh in ~/.claude/bigdad/, wired as one user-level SessionStart hook (and, on a
second yes, as the status line). Every change backs settings.json up to ~/.claude/bigdad/backups/ first and
touches only Big Dad's own entry (found by "sentinel.py" in the command). `off` takes it out again.
"""
import json, os, shutil, sys, time

HOOKS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
COPIES = ("sentinel.py", "run.sh")
MARK = "sentinel.py"


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def bd_dir():
    return os.path.join(config_dir(), "bigdad")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def fwd(p):
    return p.replace("\\", "/")


def command(mode):
    return f'sh "{fwd(bd_dir())}/run.sh" sentinel.py {mode}'


def read_settings():
    """(dict, None) or (None, why). A settings.json that is there but not valid JSON is never rewritten."""
    p = settings_path()
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError, RecursionError) as e:
        return None, f"{p} is not valid JSON ({e.__class__.__name__}); fix it first, Big Dad won't rewrite it."
    if not isinstance(d, dict):
        return None, f"{p} is not a JSON object; Big Dad won't rewrite it."
    return d, None


def backup():
    p = settings_path()
    if not os.path.isfile(p):
        return ""
    d = os.path.join(bd_dir(), "backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, time.strftime("settings-%Y%m%d-%H%M%S.json"))
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(d, time.strftime("settings-%Y%m%d-%H%M%S") + f"-{n}.json")
        n += 1
    shutil.copy2(p, dst)
    return dst


def write_settings(d):
    p = settings_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".bigdad.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)


def copy_files():
    os.makedirs(bd_dir(), exist_ok=True)
    for n in COPIES:
        shutil.copyfile(os.path.join(HOOKS, n), os.path.join(bd_dir(), n))


def ours(h):
    return isinstance(h, dict) and MARK in str(h.get("command") or "")


def hook_on(d):
    return any(ours(h) for g in ((d.get("hooks") or {}).get("SessionStart") or []) if isinstance(g, dict)
               for h in (g.get("hooks") or []))


def status_on(d):
    return ours(d.get("statusLine"))


def drop_hook(d):
    hooks = d.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("SessionStart"), list):
        return False
    before = json.dumps(hooks["SessionStart"])
    groups = []
    for g in hooks["SessionStart"]:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            g = dict(g, hooks=[h for h in g["hooks"] if not ours(h)])
            if not g["hooks"]:
                continue
        groups.append(g)
    if groups:
        hooks["SessionStart"] = groups
    else:
        del hooks["SessionStart"]
    if not hooks:
        del d["hooks"]
    return json.dumps(groups) != before


def check():
    sys.path.insert(0, HOOKS)
    import sentinel as S
    msgs = S.problems()
    for _, t in msgs:
        print("  [fix] " + t)
    if not msgs:
        print("  [ok] every enabled plugin's folder is there.")
    d, _ = read_settings()
    d = d or {}
    print(f"  sentinel hook: {'on' if hook_on(d) else 'off'} · status line: {'on' if status_on(d) else 'off'}"
          f" · last plugin load: {S.load(os.path.join(bd_dir(), 'plugin-load.json')).get('iso') or 'never seen'}")


def cmd_sentinel(rest):
    v = rest[0].lower() if rest else ""
    if v == "check":
        check()
        return
    if v not in ("on", "off"):
        d, _ = read_settings()
        print("usage: bigdad.py sentinel on     one user-level SessionStart hook, outside the plugin folder: says so when\n"
              "                                 a plugin's folder is gone or Big Dad's hooks stopped running\n"
              "       bigdad.py sentinel off    take it out again\n"
              "       bigdad.py sentinel check  run the check now")
        print(f"now: {'on' if hook_on(d or {}) else 'off'}")
        return
    d, why = read_settings()
    if d is None:
        print("Big Dad sentinel: not changed. " + why)
        return
    if v == "on":
        copy_files()
        if hook_on(d):
            print(f"Big Dad sentinel: already on (copies refreshed in {bd_dir()}).")
            return
        b = backup()
        hooks = d.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            print("Big Dad sentinel: not changed. settings.json \"hooks\" is not an object.")
            return
        hooks.setdefault("SessionStart", []).append(
            {"hooks": [{"type": "command", "command": command("session-start"), "timeout": 10}]})
        write_settings(d)
        print(f"Big Dad sentinel: on. One SessionStart hook in {settings_path()} runs {fwd(bd_dir())}/sentinel.py "
              "(a copy outside the plugin folder; Big Dad refreshes it when it updates). It prints nothing while all "
              "is well. " + (f"Backup: {b}. " if b else "") + "Off: bigdad.py sentinel off. Starts with the next session.")
        return
    if not hook_on(d):
        print("Big Dad sentinel: already off.")
        return
    b = backup()
    drop_hook(d)
    write_settings(d)
    print("Big Dad sentinel: off. Hook removed from settings.json" + (f" (backup: {b})." if b else ".")
          + (" The status line still uses it: bigdad.py statusline off." if status_on(d) else ""))


def cmd_statusline(rest):
    v = rest[0].lower() if rest else ""
    replace = "--replace" in rest
    prev_file = os.path.join(bd_dir(), "statusline-previous.json")
    if v not in ("on", "off"):
        d, _ = read_settings()
        print("usage: bigdad.py statusline on [--replace]   Big Dad's status line (💸 🧾 📊), plus a warning in it when\n"
              "                                             the plugin can't load\n"
              "       bigdad.py statusline off              take it out (puts back the one it replaced)")
        print(f"now: {'on' if status_on(d or {}) else 'off'}")
        return
    d, why = read_settings()
    if d is None:
        print("Big Dad status line: not changed. " + why)
        return
    if v == "on":
        if status_on(d):
            copy_files()
            print("Big Dad status line: already on.")
            return
        old = d.get("statusLine")
        if old and not replace:                # refuse before writing anything
            print("Big Dad status line: not changed. You already have a status line:\n  "
                  + json.dumps(old, ensure_ascii=False)
                  + "\nTo swap it for Big Dad's (yours is saved and comes back with `statusline off`): "
                    "bigdad.py statusline on --replace")
            return
        copy_files()
        b = backup()
        if old:
            with open(prev_file, "w", encoding="utf-8") as f:
                json.dump(old, f)
        d["statusLine"] = {"type": "command", "command": command("statusline"), "padding": 0}
        write_settings(d)
        print("Big Dad status line: on (shows from the next turn). " + (f"Backup: {b}. " if b else "")
              + "Off: bigdad.py statusline off.")
        return
    if not status_on(d):
        print("Big Dad status line: already off (not Big Dad's, left alone)." if d.get("statusLine") else
              "Big Dad status line: already off.")
        return
    b = backup()
    old = None
    try:
        with open(prev_file, encoding="utf-8", errors="replace") as f:
            old = json.load(f)
    except (OSError, ValueError, RecursionError):
        pass
    if old:
        d["statusLine"] = old
        os.remove(prev_file)
    else:
        del d["statusLine"]
    write_settings(d)
    print("Big Dad status line: off" + (", your previous one is back" if old else "")
          + (f" (backup: {b})." if b else "."))
