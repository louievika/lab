#!/usr/bin/env python3
"""Big Dad sentinel: says so when a plugin cannot load, from a path that does not depend on the plugin.

A plugin that never loads can't warn anyone: its hooks never run. Claude Code shows the failure only in
`claude plugin list` ("failed to load"). This file is copied (on a yes: `bigdad.py sentinel on`) to
~/.claude/bigdad/sentinel.py, next to a copy of run.sh, and runs as one user-level SessionStart hook:

  - every enabled plugin whose marketplace is a local directory: the folder (or its .claude-plugin
    manifest) is gone -> one line naming the plugin, the dead path and the fix
    (`claude plugin marketplace add <new folder>`);
  - every enabled plugin whose recorded install folder is gone -> one line (reinstall);
  - Big Dad enabled, but its own SessionStart hook did not run in the previous session (plugin-load.json,
    written by hooks/session_start.py, is missing or older than the sentinel's last run) -> one line.

Nothing when all is well (zero tokens). Output is a systemMessage: the user sees it, the model does not.
Standard library only, no imports from the plugin (the plugin may be the thing that is gone).

  python sentinel.py session-start   the hook (reads the event on stdin)
  python sentinel.py statusline      a status line: the same check, then Big Dad's own status line from the
                                     folder the plugin last loaded from (plugin-load.json "root")
  python sentinel.py check           print the findings (or "ok") for a person

  BIGDAD_SENTINEL=off   say nothing
"""
import io, json, os, runpy, sys, time

SLACK = 120          # seconds: the plugin's own SessionStart may run a little before or after this one


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def bd_dir():
    return os.environ.get("BIGDAD_HOME") or os.path.join(config_dir(), "bigdad")


def load(path, damaged=None):
    """The JSON object in path, else {}. damaged: what a file that is there but can't be read, can't be parsed or is
    not an object gives instead (thirty-sixth review: hooks_note tells it from no file); {} when None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except (OSError, ValueError, RecursionError):
        pass
    return {} if damaged is None else damaged


def enabled(cwd=""):
    """name@market -> True for every plugin turned on in the user's (and this project's) settings."""
    out = {}
    files = [os.path.join(config_dir(), n) for n in ("settings.json", "settings.local.json")]
    if cwd:
        files += [os.path.join(cwd, ".claude", n) for n in ("settings.json", "settings.local.json")]
    for p in files:
        ep = load(p).get("enabledPlugins")
        if isinstance(ep, dict):
            out.update(ep)
    return {k: v for k, v in out.items() if v is True and isinstance(k, str) and "@" in k}


def sources(cwd=""):
    """marketplace name -> its source dict, from plugins/known_marketplaces.json + settings extraKnownMarketplaces."""
    out = {}
    for n, m in load(os.path.join(config_dir(), "plugins", "known_marketplaces.json")).items():
        if isinstance(m, dict) and isinstance(m.get("source"), dict):
            out[n] = m["source"]
    files = [os.path.join(config_dir(), "settings.json")] + ([os.path.join(cwd, ".claude", "settings.json")] if cwd else [])
    for p in files:
        extra = load(p).get("extraKnownMarketplaces")
        for n, m in (extra.items() if isinstance(extra, dict) else ()):
            if isinstance(m, dict) and isinstance(m.get("source"), dict):
                out[n] = m["source"]
    return out


def problems(cwd=""):
    """[(plugin, text)] one per plugin that can't load. Cheap: a few stats and three small JSON files."""
    on = enabled(cwd)
    if not on:
        return []
    src = sources(cwd)
    inst = load(os.path.join(config_dir(), "plugins", "installed_plugins.json")).get("plugins")
    inst = inst if isinstance(inst, dict) else {}
    out = []
    for name in sorted(on):
        pname, _, market = name.partition("@")
        s = src.get(market) or {}
        if s.get("source") == "directory":
            path = s.get("path") if isinstance(s.get("path"), str) else ""
            if not path or not os.path.isdir(path):
                out.append((pname, f"plugin {pname} can't load: its marketplace folder is gone ({path or 'no path'}). "
                                   f"If you moved it: `claude plugin marketplace add \"<new folder>\"`, then restart."))
                continue
            if not any(os.path.isfile(os.path.join(path, ".claude-plugin", n)) for n in ("marketplace.json", "plugin.json")):
                out.append((pname, f"plugin {pname} can't load: {path} has no .claude-plugin/marketplace.json. "
                                   "Point the marketplace at the right folder: `claude plugin marketplace add \"<folder>\"`."))
            continue
        rows = inst.get(name)
        rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
        paths = [r.get("installPath") for r in rows if isinstance(r, dict) and isinstance(r.get("installPath"), str)]
        if paths and not any(os.path.isdir(p) for p in paths):
            out.append((pname, f"plugin {pname}: its install folder is gone. Reinstall: `claude plugin install {name}`."))
    return out


def bigdad_on(cwd=""):
    return any(k.partition("@")[0] == "bigdad" for k in enabled(cwd))


def _usable_ts(v, now):
    """A time read back from a file is usable only as a number, not a bool, with 0 < v <= now + SLACK (thirty-fourth
    review: the sentinel imports nothing of Big Dad's, so the rule lives here; a far-future load ts passed the old
    10**12 top and hid the "did not run" note for good). NaN and inf fail the comparison."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= now + SLACK


def hooks_note(state, cwd=""):
    """Big Dad is enabled but its own SessionStart did not run in the last session the sentinel saw."""
    if not bigdad_on(cwd):
        return ""
    now = time.time()
    prev = state.get("last_run")
    if not _usable_ts(prev, now):
        return ""                      # first run (or a damaged last_run: NaN, inf, future): nothing to compare against
    load_row = load(os.path.join(bd_dir(), "plugin-load.json"), damaged=False)
    seen = load_row.get("ts") if isinstance(load_row, dict) else False
    # thirty-fifth review: "never" only when no load time is on file (no file, no ts, null); one that is there but
    # not usable (future, NaN, true, a string) still warns, and says its time can't be read. Thirty-sixth review: a
    # file that is there but can't be read or parsed, or is not an object, is "time not readable" too, not "never"
    when = "never" if seen is None else "time not readable"
    if not _usable_ts(seen, now):      # thirty-first review: a NaN or negative ts raised at localtime
        seen = 0
    if seen >= prev - SLACK:
        return ""
    if seen:                           # thirty-sixth review: usable (0 < ts <= now + SLACK), localtime takes it
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(seen))
    return ("Big Dad is enabled but its hooks did not run in your last session (last loaded: "
            f"{when}). Run `claude plugin list`; if it says failed to load, re-add the marketplace or reinstall.")


def messages(cwd="", state=None):
    out = [t for _, t in problems(cwd)]
    if state is not None:
        note = hooks_note(state, cwd)
        if note and not any(p == "bigdad" for p, _ in problems(cwd)):
            out.append(note)
    return out


def read_event():
    try:
        e = json.load(sys.stdin)
        return e if isinstance(e, dict) else {}
    except (ValueError, OSError, UnicodeDecodeError, RecursionError):
        return {}


def write(text):
    data = (text + "\n").encode("utf-8")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    except (AttributeError, ValueError):
        print(text)


def session_start():
    event = read_event()
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
    sp = os.path.join(bd_dir(), "sentinel-state.json")
    state = load(sp)
    msgs = messages(cwd, state)
    state["last_run"] = time.time()
    try:
        os.makedirs(bd_dir(), exist_ok=True)
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass
    if msgs:
        write(json.dumps({"systemMessage": "🥛 Big Dad sentinel: " + " ".join(msgs)}, ensure_ascii=False))


def statusline():
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        event = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (ValueError, RecursionError):        # 3.0.0 eleventh review: too deeply nested is no event too
        event = {}
    cwd = event.get("cwd") if isinstance(event, dict) and isinstance(event.get("cwd"), str) else ""
    found = problems(cwd)
    own = [t for p, t in found if p == "bigdad"]
    if own:                                    # Big Dad itself can't load: its numbers would be stale, say why instead
        write("🥛 Big Dad ⚠ " + own[0])
        return
    root = load(os.path.join(bd_dir(), "plugin-load.json")).get("root")
    sl = os.path.join(root, "hooks", "statusline.py") if isinstance(root, str) and root else ""
    other = [t for _, t in found][:1]
    if other:                                  # another plugin is dead: one warning line, Big Dad's line below it
        write("🥛 Big Dad ⚠ " + other[0])
    if not sl or not os.path.isfile(sl):
        if not other:
            write("🥛 Big Dad: waiting for its first session start" if bigdad_on(cwd) else "")
        return
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    sys.argv = [sl]
    try:
        runpy.run_path(sl, run_name="__main__")
    except SystemExit:
        pass
    except Exception:
        write("🥛 Big Dad")


def main():
    if os.environ.get("BIGDAD_SENTINEL", "").lower() in ("off", "0", "false"):
        return
    mode = sys.argv[1] if len(sys.argv) > 1 else "session-start"
    try:
        if mode == "statusline":
            statusline()
        elif mode == "check":
            msgs = messages("", load(os.path.join(bd_dir(), "sentinel-state.json")))
            write("\n".join(msgs) if msgs else "ok: every enabled plugin's folder is there")
        else:
            session_start()
    except Exception:
        return                         # a hook must never break a session start


if __name__ == "__main__":
    main()
