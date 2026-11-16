"""Big Dad `setup`: tune Big Dad's own knobs to this machine. Writes ~/.claude/bigdad/profile.json.

Observes, never changes the user's Claude settings: today's date/time and timezone, OS, the
surfaces seen in the logs (by entrypoint), Claude Code version, models used, the real context
and auto-compact window, whether Claude Code's own auto-compact is on (and which setting says so), skills, plugins, MCP servers, hooks from every enabled plugin per event
with their launch cost when known, and the Python launcher. From that it derives Big Dad's defaults:

  guard_at        min(130k, ~65% of the real window)      override BIGDAD_GUARD_AT; a project that needs more:
                  a cost guide, not a limit                `bigdad.py guide 300k` in it (<project>/.claude/bigdad.json)
  big_model       priciest model family seen               override BIGDAD_BIG_MODEL
                  (price tiers low->high: haiku, sonnet, opus; a family not in the list counts as
                  the top tier; override the order with BIGDAD_MODEL_TIERS="haiku,sonnet,opus,...")
  max_big_agents  1 big-model background agent at a time   override BIGDAD_MAX_BIG_AGENTS

Any report runs setup by itself when the profile is missing, older than 7 days, or the
Claude Code version changed. The startup refresh (bd_refresh) rebuilds it in the background when the
setup fingerprint moved. Neither ever runs a hook: the launch cost comes from the `durationMs` Claude Code
already logs for each hook run, or "not measured". Only `setup --measure`, typed by the user, runs each
per-call hook command once with a probe event to time it.
"""
import glob, json, os, platform, re, shutil, subprocess, sys, time
import bd_health as H

BD_DIR = os.path.join(H.CLAUDE_DIR, "bigdad")
PROFILE = os.environ.get("BIGDAD_PROFILE") or os.path.join(BD_DIR, "profile.json")
STALE_DAYS = 7
GUARD_CAP = 130_000
GUARD_SHARE = 0.65
MAX_BIG_TOP = 64            # fortieth review: BIGDAD_MAX_BIG_AGENTS is a whole number 1..64, else the default 1
FAMILY_RANK = {"haiku": 1, "sonnet": 2, "opus": 3}      # price tiers, low -> high; a family not listed = top tier
PER_CALL_EVENTS = ("PreToolUse", "PostToolUse")
NETWORK = re.compile(r"curl|wget|https?://|npx|Invoke-WebRequest|\biwr\b|\birm\b", re.I)
SURFACES = {"cli": "CLI (terminal)", "sdk-cli": "headless / Agent SDK (claude -p)", "sdk-ts": "Agent SDK (TypeScript)",
            "sdk-py": "Agent SDK (Python)", "vscode": "IDE (VS Code)", "jetbrains": "IDE (JetBrains)",
            "desktop": "Desktop app", "web": "claude.ai web", "ide": "IDE"}


def family(model):
    m = re.search(r"claude-([a-z]+)", model or "")      # also a prefixed id (us.anthropic.claude-...)
    return m.group(1) if m else (model or "?")


def tiers():
    """Family -> price rank. BIGDAD_MODEL_TIERS (comma list, cheapest first) replaces the default order."""
    env = [t.strip().lower() for t in (os.environ.get("BIGDAD_MODEL_TIERS") or "").split(",") if t.strip()]
    return {f: i + 1 for i, f in enumerate(env)} if env else dict(FAMILY_RANK)


def rank(model):
    t = tiers()
    return t.get(family(model).lower(), max(t.values()) + 1)


def is_small(model):
    """True for the cheapest tier only (the one to name for lookups)."""
    return rank(model) == min(tiers().values())


def model_window(model):
    """Context window Claude Code gives a model id by default (docs, model-config): 1M for the
    Fable models, Sonnet 5+, Haiku 5.5+, Opus 4.7+ and any [1m] variant; else 200k.
    CLAUDE_CODE_DISABLE_1M_CONTEXT=1 keeps every session at 200k."""
    if os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT") == "1":
        return 200_000
    m = model or ""
    if "[1m]" in m:
        return 1_000_000
    fam = family(m)
    v = re.search(r"-(\d+)-(\d+)", m)
    ver = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    if fam == "fable" or (fam == "sonnet" and ver >= (5, 0)) or (fam == "haiku" and ver >= (5, 5)) or (fam == "opus" and ver >= (4, 7)):
        return 1_000_000
    return 200_000


def parse_window(v):
    """100000 / '200k' / '1M' -> int tokens, or 0. Fortieth review: a bool, NaN, Infinity (1e400), a negative or a
    number past bd_common.NUM_TOP reads as not set (0), through bd_common.sane (int(inf) raised in build())."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(_bc().sane(v, 0))
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([kKmM])?\s*$", str(v or ""))
    if not m:
        return 0
    n = float(m.group(1)) * {None: 1, "k": 1e3, "m": 1e6}[(m.group(2) or "").lower() or None]
    return int(_bc().sane(n, 0))       # a 400-digit text is float inf: 0 too


def logs_recent(days=7, limit=400):
    out = []
    since = time.time() - days * 86400
    for p in glob.glob(os.path.join(glob.escape(H.CLAUDE_DIR), "projects", "**", "*.jsonl"), recursive=True):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt >= since:
            out.append((mt, p))
    out.sort(reverse=True)
    return [p for _, p in out[:limit]]


def observe_logs(days=7):
    """Entrypoints (surfaces) and models seen, with call counts; cheap: first 300 lines per log for the entrypoint."""
    surfaces, models, other = {}, {}, 0
    for p in logs_recent(days):
        entry = None
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if entry is None:
                        m = re.search(r'"entrypoint":\s*"([^"]+)"', line)
                        if m:
                            entry = m.group(1)
                    if '"usage"' in line:
                        m = re.search(r'"model":\s*"([^"]*?claude-[^"]+)"', line)
                        if m:
                            models[m.group(1)] = models.get(m.group(1), 0) + 1
                        else:
                            try:
                                msg = json.loads(line).get("message") or {}
                            except (ValueError, AttributeError, RecursionError):
                                msg = {}
                            mid = msg.get("model") if isinstance(msg, dict) else None
                            if isinstance(mid, str) and mid and mid != "<synthetic>":
                                other += 1             # a call whose model id names no Claude family
                    if i > 300 and entry is not None and i > 3000:
                        break
        except OSError:
            continue
        if "/subagents/" not in p.replace("\\", "/"):
            surfaces[entry or "cli"] = surfaces.get(entry or "cli", 0) + 1
    return surfaces, models, other


def window_sources(cwd, models):
    """(window, source, settings file or None) for the real auto-compact window, in the order Claude Code
    applies them. The file is set only when autoCompactWindow in it is what applies."""
    env = parse_window(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    if env:
        return env, "CLAUDE_CODE_AUTO_COMPACT_WINDOW", None
    for p in reversed(H.settings_files(cwd)):          # managed, then local/project, then user
        s = H.load_json(p)
        w = parse_window(s.get("autoCompactWindow"))
        if w:
            if H.is_managed(p):
                return w, f"autoCompactWindow in {H.where(p)}, {H.ORG}", p
            return w, f"autoCompactWindow in {H.rel(p)}", p
    if models:
        top = max(models.items(), key=lambda kv: kv[1])[0]
        return model_window(top), f"context window of the model used most ({family(top)})", None
    return 200_000, "default (no setting, no logs)", None


def _key(p):
    return os.path.normcase(os.path.abspath(p))


def window_files(cwd):
    """{settings file: its autoCompactWindow, 0 = not set or no file}: Big Dad's record of what it read, kept in the
    profile so the next build can tell whether the value really changed."""
    return {_key(p): parse_window(H.load_json(p).get("autoCompactWindow")) for p in H.settings_files(cwd, False)}


def window_change(prev, files, src):
    """{"file", "was", "after"} when the last profile read the same settings file and saw another autoCompactWindow:
    the value changed after `after` (that profile's time). Kept while the value stays; else {}. A file's modified
    time is never the evidence: any write (a plugin install, another setting) moves it."""
    if not src or not isinstance(prev, dict):
        return {}
    seen, k = prev.get("window_files"), _key(src)
    if not isinstance(seen, dict) or type(seen.get(k)) is not int or k not in files:
        return {}                                      # no readable record of this file: no evidence
    if seen[k] == files[k]:
        old = prev.get("window_change")
        return old if isinstance(old, dict) and old.get("file") == k else {}
    try:
        after = int(time.mktime(time.strptime(str(prev.get("at", ""))[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError, OverflowError, OSError):   # fortieth review: OSError too, as stale()
        return {}
    return {"file": k, "was": seen[k], "after": after}


def session_start(path):
    """Epoch of a transcript's first timestamped line, else its ctime; 0 if unreadable."""
    import calendar
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                m = re.search(r'"timestamp":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)', line)
                if m:
                    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
        return int(os.path.getctime(path))
    except (OSError, ValueError, OverflowError):
        return 0


def sessions_before(set_at, minutes=30):
    """Count of sessions still open (main transcript written in the last `minutes`) that started
    before `set_at`: autoCompactWindow does not apply to them until they are restarted."""
    if not set_at:
        return 0
    n = 0
    cut = time.time() - minutes * 60
    for p in glob.glob(os.path.join(glob.escape(H.CLAUDE_DIR), "projects", "*", "*.jsonl")):
        try:
            if os.path.getmtime(p) < cut:
                continue
        except OSError:
            continue
        start = session_start(p)
        if start and start < set_at:
            n += 1
    return n


def window_note(p):
    """One line for setup/agents when open sessions started before Big Dad's last profile, which saw another
    autoCompactWindow in the same settings file (window_change), else ''. No record of a change: no flag."""
    ch = p.get("window_change")
    after = ch.get("after") if isinstance(ch, dict) else None
    if type(after) is not int or after <= 0:
        return ""
    n = sessions_before(after)
    if not n:
        return ""
    was = ch.get("was")
    was = f"still {was // 1000:,}k" if type(was) is int and was > 0 else "not set yet"
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(after))
    except (OverflowError, OSError, ValueError):   # fortieth review: a time localtime can't take (10**12 on Windows)
        return ""                                  # is no usable record: no flag, as no record
    return (f"flag: {n} open session(s) started before autoCompactWindow changed (at {when} Big Dad saw it {was}):"
            " the setting only protects sessions started after it is set. Restart those sessions for it to apply.")


def python_launcher():
    try:
        return open(os.path.join(BD_DIR, "python-cmd"), encoding="utf-8", errors="replace").read().strip() or "?"
    except OSError:
        return "(not cached yet: first hook run picks it)"


def short_cmd(cmd):
    """A hook command without paths or quotes: `sh run.sh context_guard.py`."""
    # thirty-seventh review, part 3: a command that is not a str (a number from a log raised in re) reads as ''
    toks = re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', cmd if isinstance(cmd, str) else "")
    out = []
    for tok in toks:
        t = next(x for x in tok if x)
        out.append(t.replace("\\", "/").rsplit("/", 1)[-1])
    return " ".join(out)[:70]


def hooks_observed(days=7, limit=60):
    """Hook commands Claude Code itself recorded running (system hook-summary rows in recent
    main transcripts, every plugin incl. bundled ones): runs and the median duration when logged."""
    seen = {}
    for p in logs_recent(days, limit):
        if "/subagents/" in p.replace("\\", "/"):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"hookInfos"' not in line and '"durationMs"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    infos = d.get("hookInfos") if isinstance(d, dict) else None
                    att = d.get("attachment") if isinstance(d, dict) else None
                    # thirty-seventh review, part 3: a subtype, event or command that is not a str reads as missing
                    # (str(["x"]) was an event name; a number command raised in short_cmd); hookInfos not a list: none
                    sub = d.get("subtype") if isinstance(d, dict) and isinstance(d.get("subtype"), str) else ""
                    ev = "".join(w.capitalize() for w in sub.replace("_hook_summary", "").split("_"))
                    if isinstance(att, dict) and att.get("command"):
                        infos, ev = [att], (att.get("hookEvent") if isinstance(att.get("hookEvent"), str) else "")
                    for h in infos if isinstance(infos, list) else []:
                        if not isinstance(h, dict) or not isinstance(h.get("command"), str) or not h["command"]:
                            continue
                        key = (ev, short_cmd(h["command"]))
                        s = seen.setdefault(key, {"runs": 0, "ms": []})
                        s["runs"] += 1
                        if isinstance(h.get("durationMs"), int):
                            s["ms"].append(h["durationMs"])
        except OSError:
            continue
    out = []
    for (ev, cmd), s in sorted(seen.items(), key=lambda kv: -kv[1]["runs"]):
        ms = sorted(s["ms"])
        out.append({"event": ev, "cmd": cmd, "runs": s["runs"], "ms": ms[len(ms) // 2] if ms else None})
    return out[:40]


def measure_hooks(cwd, plugins, enabled, measure=False, observed=None):
    """Every command hook per event (settings files + enabled plugins). measure=False (the default, and every
    automatic path): no hook runs; `ms` is the median `durationMs` Claude Code logged for that command
    (`ms_from` "logs"), else None. measure=True (only `setup --measure`): each per-tool-call command runs once
    with a probe event and `ms` is that time (`ms_from` "measure"). Network-looking commands are never run."""
    sfiles = H.settings_loaded(cwd)
    logged = {(o["event"], o["cmd"]): o["ms"] for o in (observed or []) if o.get("ms") is not None}
    per_event = {}
    sh = shutil.which("sh")
    probe = json.dumps({"hook_event_name": "PostToolUse", "session_id": "bigdad-setup-probe", "tool_name": "BigDadProbe",
                        "tool_input": {}, "tool_response": "", "cwd": cwd}).encode()
    for where, cmd, root in H.hook_commands(sfiles, plugins, enabled):
        src, event = where.rsplit(" ", 1)
        row = {"from": src, "cmd": short_cmd(cmd), "ms": None}
        if not measure and (event, row["cmd"]) in logged:
            row["ms"], row["ms_from"] = logged[(event, row["cmd"])], "logs"
        if measure and sh and event in PER_CALL_EVENTS + ("UserPromptSubmit",) and not NETWORK.search(cmd):
            # the same command string Claude Code hands to `sh` on every event, run once with a probe event
            full = cmd.replace("${CLAUDE_PLUGIN_ROOT}", root or "").replace("$CLAUDE_PLUGIN_ROOT", root or "")
            env = dict(os.environ, CLAUDE_PLUGIN_ROOT=root or "", BIGDAD_PROBE="1", BIGDAD_GUARD="off", BIGDAD_WATCH="off")
            t = time.perf_counter()
            try:
                subprocess.run([sh, "-c", full], input=probe, capture_output=True, env=env, timeout=5)
                row["ms"], row["ms_from"] = int((time.perf_counter() - t) * 1000), "measure"
            except (OSError, subprocess.SubprocessError):
                row["ms"] = -1
        per_event.setdefault(event, []).append(row)
    return per_event


def per_call_cost(per_call, measured):
    """hooks_per_tool_call: count, the summed launch cost of the commands whose cost is known, how many are not
    known, and where the known numbers came from ("setup --measure", "Claude Code's logs" or None)."""
    known = [r for r in per_call if (r.get("ms") or 0) > 0]
    src = None
    if known:
        src = "setup --measure" if measured else "Claude Code's logs"
    return {"count": len(per_call), "ms": sum(r["ms"] for r in known), "unmeasured": len(per_call) - len(known), "source": src}


def launch_cost_text(hp):
    """The one true phrase for the hook launch cost, used by setup, health and the report."""
    hp = hp if isinstance(hp, dict) else {}             # thirty-ninth review: not a dict reads as missing
    BC = _bc()
    # fortieth review: ms and the gap read through sane, as health judges them: one that fails it (10**13, "5000",
    # NaN, a bool) is not measured here too, never printed raw
    src, ms, gap = hp.get("source"), BC.sane(hp.get("ms"), 0), BC.sane(hp.get("unmeasured"), 0, whole=True)
    if "source" not in hp and ms:                        # a profile from before 3.0.0, which timed hooks itself
        return f"~{ms} ms launch cost measured by an earlier setup"
    if not src or not ms:
        return "launch cost not measured (`setup --measure` runs each per-call hook once to time it)"
    if src == "setup --measure":
        return f"~{ms} ms launch cost measured by `setup --measure`" + (f" ({gap} not measured)" if gap else "")
    return f"~{ms} ms launch cost from Claude Code's logs" + (f" ({gap} not in the logs)" if gap else "")


WINDOW_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_DISABLE_1M_CONTEXT", "BIGDAD_GUARD_AT")
COMPACT_ENV = H.COMPACT_OFF_ENV + ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",)


def _bc():
    """The hooks' bd_common (one rule for BIGDAD_GUARD_AT lives there: env_guide)."""
    hooks = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import bd_common
    return bd_common


def guard_env_ignored(p):
    """The BIGDAD_GUARD_AT text when it is set but not usable, else ''. Thirty-fifth review: p is the profile
    build() made, whose guard_source says whether env_guide took the value, so the variable is parsed once."""
    raw = (os.environ.get("BIGDAD_GUARD_AT") or "").strip()
    return raw if raw and p.get("guard_source") != "BIGDAD_GUARD_AT" else ""


def shown_env(raw):
    """bd_common.shown_env (thirty-fifth review: the one rule lives there, for setup, agents and models). Called
    through _bc(), not bound at import: bd_models imports this module and loads without bd_common (17th review)."""
    return _bc().shown_env(raw)


def fingerprint(cwd):
    """A short hash of what the profile is built from: Claude Code version, skills, plugins (version, on/off),
    MCP servers, hook commands and the window settings. Reads config files only, never a log."""
    import hashlib
    BC = _bc()
    plugins = H.installed_plugins()
    enabled = H.enabled_plugins(cwd)
    parts = {
        "claude": BC.claude_version(),
        "skills": sorted(H.skill_dirs(plugins, enabled)),
        "plugins": sorted(f"{n} {v} {bool(enabled.get(n, False))}" for n, (_, v, _) in plugins.items()),
        "mcp": sorted(H.mcp_servers(cwd)),
        "hooks": sorted(f"{w} {c}" for w, c, _ in H.hook_commands(H.settings_loaded(cwd), plugins, enabled)),
        "window": [os.environ.get(k, "") for k in WINDOW_ENV]
                  + [str(s.get("autoCompactWindow", "")) for _, s in H.settings_loaded(cwd)],
        "compact": [os.environ.get(k, "") for k in COMPACT_ENV]
                   + [str(s.get("autoCompactEnabled", "")) for _, s in H.settings_loaded(cwd)]
                   + [str((s.get("env") or {}).get(k, "") if isinstance(s.get("env"), dict) else "")
                      for _, s in H.settings_loaded(cwd) for k in H.COMPACT_OFF_ENV],
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def build(cwd, measure=False):
    plugins = H.installed_plugins()
    enabled = H.enabled_plugins(cwd)
    surfaces, models, unrecognised = observe_logs()
    window, wsrc, wfile = window_sources(cwd, models)
    wfiles = window_files(cwd)
    wchange = window_change(load(), wfiles, wfile)
    seen_big = max(models, key=lambda m: (rank(m), models[m])) if models else ""
    big = os.environ.get("BIGDAD_BIG_MODEL") or family(seen_big) or "?"
    guard_env = _bc().env_guide()       # thirty-third review: an unusable value (-5, 10**13) is ignored, as unset
    guard = guard_env or min(GUARD_CAP, int(window * GUARD_SHARE))
    try:
        max_big = int(os.environ.get("BIGDAD_MAX_BIG_AGENTS") or 1)
    except ValueError:                  # also a value past Python's int digit limit
        max_big = 1
    if not 1 <= max_big <= MAX_BIG_TOP:  # fortieth review: checked once here, as env_guide does BIGDAD_GUARD_AT, so
        max_big = 1                      # setup, agents and the report all read the same number (0, -5, 10**13: 1)
    observed = hooks_observed()
    hooks = measure_hooks(cwd, plugins, enabled, measure, observed)
    per_call = [r for ev in PER_CALL_EVENTS for r in hooks.get(ev, [])]
    import bd_fresh
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tz": time.strftime("%Z %z"),
        "os": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
        "claude": bd_fresh.claude_version(),
        "surfaces": {SURFACES.get(k, k): v for k, v in sorted(surfaces.items(), key=lambda kv: -kv[1])},
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1])), "models_unrecognised": unrecognised,
        "window": window, "window_source": wsrc, "window_files": wfiles, "window_change": wchange,
        "auto_compact": H.auto_compact(cwd),
        "managed": [H.where(f) for f in H.managed_files()], "managed_hooks_only": H.managed_hooks_only(cwd),
        "guard_at": guard, "guard_source": "BIGDAD_GUARD_AT" if guard_env else f"min({GUARD_CAP // 1000}k, {int(GUARD_SHARE * 100)}% of {window // 1000}k)",
        "big_model": big, "big_model_source": "BIGDAD_BIG_MODEL" if os.environ.get("BIGDAD_BIG_MODEL") else "priciest family seen in the logs",
        "max_big_agents": max_big,
        "skills": len(H.skill_dirs(plugins, enabled)),
        "plugins": {n.split("@")[0]: (v if enabled.get(n, False) else f"{v} (off)") for n, (_, v, _) in plugins.items()},
        "mcp": len(H.mcp_servers(cwd)),
        "mcp_in_logs": mcp_in_logs(),
        "hooks": hooks,
        "hooks_observed": observed,
        "hooks_per_tool_call": per_call_cost(per_call, measure),
        "python_launcher": python_launcher(),
        "hooks_measured": bool(measure),
        "fingerprint": fingerprint(cwd),
    }


def mcp_in_logs():
    """MCP servers seen in tool calls across the logs `skills` already scanned (usage-cache.json), incl. claude.ai
    connectors, which never appear in local config. 0 until `skills` or `all` has run once."""
    try:
        with open(os.path.join(H.CLAUDE_DIR, "bigdad", "usage-cache.json"), encoding="utf-8", errors="replace") as f:
            cache = json.load(f)
    except (OSError, ValueError, RecursionError):
        return 0
    seen = set()
    for v in cache.values() if isinstance(cache, dict) else ():
        if isinstance(v, list) and len(v) > 3 and isinstance(v[3], dict):
            seen.update(v[3])
    return len(seen)


def load():
    return H.load_json(PROFILE)


def stale(p=None):
    """Why the profile needs a refresh, or '' when it is current."""
    p = p if p is not None else load()
    if not p:
        return "no profile yet"
    try:
        at = p.get("at") if isinstance(p.get("at"), str) else ""    # thirty-ninth review: a dict raised KeyError
        age = (time.time() - time.mktime(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (ValueError, TypeError, OverflowError, OSError):   # fortieth review: a year mktime can't take (9999, 0001)
        return "profile has no date"
    if age > STALE_DAYS:
        return f"profile is {age:.0f} days old"
    # an unmeasured hook launch cost is not a reason: rebuilding never runs a hook (only `setup --measure` does)
    import bd_fresh
    v = bd_fresh.claude_version()
    if v and p.get("claude") and v != p.get("claude"):
        lb = _bc().label            # fortieth review: both versions printed through label, not raw
        return f"Claude Code changed {lb(p.get('claude'))} -> {lb(v)}"
    return ""


def ensure(cwd=None, quiet=False):
    """Run setup when the profile is missing or stale; returns the profile. Never runs a hook command:
    reports say "read-only", so the launch cost comes from Claude Code's logs or stays "not measured"."""
    why = stale()
    if why:
        if not quiet:
            print(f"  (setup: {why}; tuning Big Dad to this machine first)")
        return cmd_setup(cwd, brief=True, measure=False)
    return load()


def cmd_setup(cwd=None, brief=False, measure=False):
    cwd = cwd or os.getcwd()
    BC = _bc()              # thirty-eighth review: bd_common bound once here; its helpers taken from BC below
    sh, lb, ds, dg = BC.shown, BC.label, BC.dstr, BC.dget
    p = build(cwd, measure)
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    with open(PROFILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)
    # thirty-seventh review: the version (claude-version.json or `claude --version`), printed safely through sh
    print(f"🥛 == SETUP  {p['at'][:16].replace('T', ' ')} {sh(p['tz'])} | {p['os']} | Claude Code {sh(p['claude']) or '?'} | profile written (Big Dad's own knobs only)")
    print(f"  real window: {p['window'] // 1000:,}k tokens ({p['window_source']}) -> guard at {p['guard_at'] // 1000:,}k per call ({p['guard_source']})")
    bad = guard_env_ignored(p)
    if bad:
        print(f"  BIGDAD_GUARD_AT={shown_env(bad)} is not usable (a whole number of tokens from 1 to "
              f"{BC.GUIDE_TOP:,}), so it was ignored.")
    pg, pf = BC.project_guide(cwd)
    mine = p["guard_source"] == "BIGDAD_GUARD_AT"    # build() records it only when the env value was used
    if pg:
        print(f"  this project: guide {pg // 1000:,}k per call ({sh(pf)}; `bigdad.py guide auto` drops it); it is used here"
              + (", but BIGDAD_GUARD_AT is set and wins" if mine else " unless BIGDAD_GUARD_AT is set"))
    # thirty-fifth review: BIGDAD_BIG_MODEL is environment text, printed through shown_env (stored as it was)
    print(f"  big model: {shown_env(p['big_model'])} ({p['big_model_source']}); default: {p['max_big_agents']} big-model background agent at a time")
    ac = p.get("auto_compact") or {}
    off = H.compact_off_line(ac)
    print("  " + (off or f"auto-compact on ({ac.get('source', 'default')}): Claude Code compacts by itself;"
                         " Big Dad only adds instructions on what to keep"))
    note = window_note(p)
    if note:
        print("  " + note)
    if p.get("managed"):
        print("  managed settings, set by your organisation (read-only; Big Dad proposes no cut for them): " + ", ".join(p["managed"])
              + ("; allowManagedHooksOnly is on: only managed hooks run, Big Dad's own hooks included only if your admin manages the plugin"
                 if p.get("managed_hooks_only") else ""))
    # thirty-sixth review: surfaces, model families, hook events and commands come from logs and settings: printed
    # through bd_common.shown (the one rule)
    print("  surfaces seen (7 days): " + (", ".join(f"{sh(k)} x{v}" for k, v in p["surfaces"].items()) or "none"))
    odd = p.get("models_unrecognised") or 0
    seen = ", ".join(f"{sh(family(k))} {v}" for k, v in list(p["models"].items())[:6])
    if not seen and odd:
        seen = (f"none recognised (custom back end?): {odd} calls with a model id that names no Claude family,"
                " so the window and guide use the defaults")
    elif odd:
        seen += f" (+{odd} calls with an unrecognised model id)"
    print("  models seen (7 days, calls): " + (seen or "none"))
    hp = p["hooks_per_tool_call"]
    print(f"  setup: {p['skills']} skill folders, {len(p['plugins'])} plugins, {p['mcp']} MCP servers configured here ({p.get('mcp_in_logs', 0)} seen in logs, incl. claude.ai connectors); hooks per tool call: {hp['count']} commands, "
          + launch_cost_text(hp))
    if not brief:
        # thirty-seventh review, part 3: a hook row that is not a dict, or a cmd, from or event that is not a str, reads
        # as missing, printed through label ('?'; a number cmd raised on the slice); runs through sane (0 if not a count).
        # Thirty-eighth review: `from` is a name in a row too, so label ('?'), not shown (was blank)
        for ev, rows in sorted(p["hooks"].items()):
            print(f"    {sh(ev)}: " + "; ".join(f"{lb(ds(dg(r, 'from')))} {lb(ds(dg(r, 'cmd'))[:28])}"
                                         + ("" if dg(r, "ms") is None else f" {sh(str(dg(r, 'ms')))} ms")
                                         + (" (logged)" if dg(r, "ms_from") == "logs" else "") for r in rows))
        obs = p["hooks_observed"]
        if obs:
            print(f"  hooks Claude Code logged running (7 days, every plugin incl. bundled): {len(obs)} distinct commands")
            for r in obs[:8]:
                print(f"    {lb(ds(dg(r, 'event'))):<14} {lb(ds(dg(r, 'cmd'))[:40]):<40}"
                      f" {BC.sane(dg(r, 'runs'), 0, whole=True):>5} runs" + (f"  ~{sh(str(dg(r, 'ms')))} ms" if dg(r, "ms") else ""))
        print(f"  python launcher for hooks: {p['python_launcher']}")
    try:
        on = BC.stop_at()              # thirty-fifth review: the shared code loaded one way, _bc()
    except Exception:
        on = 0
    if on:
        print(f"  runaway agents: warned at the guide, every call past 1.5x; hard stop ON at {on:g}x (off: bigdad.py agent-stop off)")
    else:
        print("  runaway agents: warned at the guide, every call past 1.5x. Native first: `autoCompactWindow` in a heavy"
              " helper's frontmatter (restart open sessions). Fallback, your call: `bigdad.py agent-stop 2` (a helper past"
              " 2x can only write its handoff and report back). Off by default.")
    try:
        st = H.sentinel_status()
    except Exception:
        st = {"hook": False, "statusline": False}
    if not st["hook"]:
        print("  sentinel: off. A plugin that does not load can't warn you (a moved folder is silent). Proposal: one user-level"
              " SessionStart hook outside the plugin folder that says so. Your call: `bigdad.py sentinel on`.")
    if not st["statusline"]:
        print("  status line: off (💸 🧾 📊 each turn). Your call: `bigdad.py statusline on` (keeps yours unless --replace).")
    print("  Override: BIGDAD_GUARD_AT, BIGDAD_BIG_MODEL, BIGDAD_MAX_BIG_AGENTS; per project: `bigdad.py guide 300k`."
          " Nothing in your Claude settings was changed.")
    return p
