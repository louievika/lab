#!/usr/bin/env python3
"""Big Dad SessionStart: Big Dad is known from the first call, and the first receipt is the baseline.

- Puts nothing into Claude's context. Through 1.6 it said "Big Dad is on: guide 130k tokens per call."; the receipts
  bench measured that line making Haiku think on a plain task (4/5 runs vs 0/5 without it, output tokens ~2x
  native, audit/bench/round-2-ablation.md). Every note names its own numbers; the rules live in SKILL.md.
- Claude Code changed since the last `fresh` (installed `claude --version` vs inventory.json, no network): one
  systemMessage to run `bigdad.py fresh`, once per new version. `--version` runs only when the claude binary's
  mtime moved (measured 2026-11-08 on Windows: ~22 ms direct, ~40 ms via a shell; the stat is under 2 ms).
- Tells the user once: the baseline receipt (re-sent share, startup load, chats past the guide, tokens re-sent
  above it). No baseline yet: it is built in the background from the logs Claude Code already keeps (never blocks
  startup), or Big Dad watches first when there are no logs.
- Every startup and /clear (2.0.6): one background refresh (bigdad.py refresh: detached, no console window, one at
  a time) rebuilds the profile only when the setup moved and the baseline from changed logs only. With
  BIGDAD_REFRESH=off, the 2.0.5 way: the baseline is rebuilt in the background when it is 7+ days old.
- No usable baseline at the first 2.0.6 start (2.0.6): Big Dad learns first (bd_learn.py): the hooks only count
  until the baseline covers enough calls and a profile exists; then one line says it is on.
- Writes ~/.claude/bigdad/plugin-load.json (time, version, the folder it ran from): the sentinel
  (hooks/sentinel.py, opt-in) reads it to say so when the hooks stop loading. If the sentinel's stable copies
  in ~/.claude/bigdad/ exist and differ from this version's, they are refreshed (they update themselves).

  BIGDAD_BASELINE=off       no baseline work at startup (no refresh either)
  BIGDAD_BASELINE_SYNC=1    build / refresh in this process (tests)
  BIGDAD_REFRESH=off        no refresh at every startup; only the 7-day baseline rebuild (and no learning)
  BIGDAD_LEARN=off          no learning period: act at once (skills/bigdad/scripts/bd_learn.py)
  BIGDAD_VERSION_CHECK=off  no Claude Code version compare
"""
import json, os, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

SCRIPTS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "bigdad", "scripts"))


LOCK_STALE = 600          # s: a refresh / build lock older than this is a crashed run


def busy():
    """True while a refresh or baseline build holds bd_dir()/baseline.building (younger than LOCK_STALE)."""
    try:
        return time.time() - os.path.getmtime(os.path.join(C.bd_dir(), "baseline.building")) < LOCK_STALE
    except OSError:
        return False


def detached(args, cwd=None):
    """Start `bigdad.py <args>` detached, no console window, output nowhere."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200       # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    if cwd and os.path.isdir(cwd):
        kw["cwd"] = cwd
    try:
        subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "bigdad.py"), *args], **kw)
    except OSError:
        pass


def build_in_background():
    """BIGDAD_REFRESH=off: `bigdad.py baseline --save --quiet` when the baseline is stale; one at a time."""
    if busy():
        return
    try:
        os.makedirs(C.bd_dir(), exist_ok=True)
        open(os.path.join(C.bd_dir(), "baseline.building"), "w").close()
    except OSError:
        return
    detached(["baseline", "--save", "--quiet"])


def refresh_in_background(cwd):
    """`bigdad.py refresh --cwd <cwd> --quiet`, unless one is running (the child takes the lock itself)."""
    if not busy():
        detached(["refresh", "--cwd", cwd, "--quiet"], cwd)


def installed_version():
    """The installed Claude Code version (bd_common.claude_version: cached by the binary's path + mtime)."""
    return C.claude_version()


def version_note():
    """'Claude Code changed (X -> Y): run `bigdad.py fresh`.' once per new version, when the last `fresh` saw X."""
    if os.environ.get("BIGDAD_VERSION_CHECK") == "off":
        return None
    last = C.dstr(C.load_json(os.environ.get("BIGDAD_INVENTORY") or os.path.join(C.bd_dir(), "inventory.json")).get("claude"))
    if not last:
        return None                    # no `fresh` yet: nothing to compare against, say nothing
    now = installed_version()
    if not now or now == last:
        return None
    told = os.path.join(C.bd_dir(), "claude-version.json")
    st = C.load_json(told)
    if st.get("told") == now:
        return None
    st["told"] = now
    try:
        with open(told, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass
    return f"🥛 Big Dad: Claude Code changed ({last} → {now}): run `bigdad.py fresh`."


HOOKS = os.path.dirname(os.path.abspath(__file__))
STABLE = ("sentinel.py", "run.sh")


def mark_loaded(session=""):
    """Record that the plugin's hooks ran (the sentinel compares this with its own last run)."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(HOOKS)
    meta = C.load_json(os.path.join(os.path.dirname(HOOKS), ".claude-plugin", "plugin.json"))
    row = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": C.dstr(meta.get("version")),
           "root": root, "session": session}
    tmp = os.path.join(C.bd_dir(), "plugin-load.json.tmp")
    try:
        os.makedirs(C.bd_dir(), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f)
        os.replace(tmp, os.path.join(C.bd_dir(), "plugin-load.json"))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
    for name in STABLE:                # self-updating: only copies the user already said yes to
        dst, src = os.path.join(C.bd_dir(), name), os.path.join(HOOKS, name)
        try:
            with open(src, "rb") as a, open(dst, "rb") as b:
                if a.read() == b.read():
                    continue
            shutil.copyfile(src, dst)
        except OSError:
            pass


def main(event=None, standalone=True):
    if C.off("BIGDAD_GUARD"):
        mark_loaded()                  # off on purpose is still "loaded": the sentinel must not cry wolf
        return
    event = C.read_event(need_state=False) if event is None else event
    if event is None:
        mark_loaded()
        return
    if C.state_problem():              # 2.0.9: no private state folder; the other hooks stay idle, so say why, once here
        mark_loaded(C.dstr(event.get("session_id")))
        return C.emit({"systemMessage": C.idle_line()}, standalone)
    source = C.dstr(event.get("source")) or "startup"
    mark_loaded(C.dstr(event.get("session_id")))
    if source == "compact" and C.dstr(event.get("session_id")):
        flag = C.redirect_file(C.dstr(event.get("session_id")))
        if os.path.exists(flag):
            with C.lock(flag):
                st = C.load_json(flag)
                if C.settle_dropped(st, done=True):
                    try:
                        with open(flag, "w", encoding="utf-8") as f:
                            json.dump(st, f)
                    except OSError:
                        pass
    cwd = C.dstr(event.get("cwd")) or os.getcwd()
    limit, pause = C.guard_at(), C.pause_at()
    on = f" · pause {pause:g}x" if pause else ""
    out = {}
    if source in ("startup", "clear") and os.environ.get("BIGDAD_BASELINE") != "off":
        sys.path.insert(0, SCRIPTS)
        import bd_baseline as B
        b = B.load()
        learn = not C.off("BIGDAD_REFRESH") and not C.off("BIGDAD_LEARN")
        if learn:
            import bd_learn
            bd_learn.begin(b)          # decided once; a baseline from before 2.0.6 means it never starts
        sync = os.environ.get("BIGDAD_BASELINE_SYNC") == "1"
        if C.off("BIGDAD_REFRESH"):
            if sync and B.stale(b):
                b = B.rebuild()
            elif B.stale(b):
                build_in_background()
        elif sync:
            import bd_refresh
            bd_refresh.run(cwd)
            b = B.load()
        else:
            refresh_in_background(cwd)
        told = None
        if learn:
            bd_learn.end_if_ready()
            told = C.learned_note()
        if told:
            out["systemMessage"] = told
        elif learn and C.learning():
            out["systemMessage"] = (f"🥛 Big Dad is learning first: it watches and counts, and changes nothing until "
                                    f"your baseline covers {bd_learn.min_calls()} calls; one line says when it is on.")
        elif b and not b.get("shown"):
            out["systemMessage"] = B.one_line(b)
            if not b.get("watching"):
                b["shown"] = True
                try:
                    B.save(b)
                except OSError:        # held open by the refresh (Windows) or not writable: shown again next start
                    pass
        elif not b:
            out["systemMessage"] = ("🥛 Big Dad is on. First run: building your baseline from your past logs in the "
                                    "background; it shows at your next session start (or run `bigdad.py baseline`).")
        else:
            guide = f"guide {limit // 1000}k/call ({C.guard_source()})"
            out["systemMessage"] = (f"🥛 Big Dad on · {guide}{on} · baseline 🧾 "
                                    f"{b.get('resent_share', 0):.0%} re-sent" if not b.get("watching") else
                                    f"🥛 Big Dad on · {guide} · watching first")
        try:
            import bd_settle
            grew = bd_settle.unshown_line()
            if grew:
                out["systemMessage"] = (out.get("systemMessage", "") + " " + grew).strip()
        except Exception:
            pass
    vnote = version_note()
    if vnote:
        out["systemMessage"] = (out.get("systemMessage", "") + " " + vnote).strip()
    return C.emit(out, standalone) if out else None


if __name__ == "__main__":
    main()
