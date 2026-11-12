"""Big Dad `setup`: tune Big Dad's own knobs to this machine. Writes ~/.claude/bigdad/profile.json.

Observes, never changes the user's Claude settings: today's date/time and timezone, OS, the
surfaces seen in the logs (by entrypoint), Claude Code version, models used, the real context
and auto-compact window, skills, plugins, MCP servers, hooks from every enabled plugin per event
with a measured launch cost, and the Python launcher. From that it derives Big Dad's defaults:

  guard_at        min(130k, ~65% of the real window)      override BIGDAD_GUARD_AT
  big_model       priciest model family seen               override BIGDAD_BIG_MODEL
                  (price tiers low->high: haiku, sonnet, opus; a family not in the list counts as
                  the top tier; override the order with BIGDAD_MODEL_TIERS="haiku,sonnet,opus,...")
  max_big_agents  1 big-model background agent at a time   override BIGDAD_MAX_BIG_AGENTS

Any report runs setup by itself when the profile is missing, older than 7 days, or the
Claude Code version changed. The startup refresh (bd_refresh) rebuilds it in the background when the
setup fingerprint moved, without running any hook; the next report then measures the hook launch cost.
"""
import glob, json, os, platform, re, shutil, subprocess, sys, time
import bd_health as H

BD_DIR = os.path.join(H.CLAUDE_DIR, "bigdad")
PROFILE = os.environ.get("BIGDAD_PROFILE") or os.path.join(BD_DIR, "profile.json")
STALE_DAYS = 7
GUARD_CAP = 130_000
GUARD_SHARE = 0.65
FAMILY_RANK = {"haiku": 1, "sonnet": 2, "opus": 3}      # price tiers, low -> high; a family not listed = top tier
PER_CALL_EVENTS = ("PreToolUse", "PostToolUse")
NETWORK = re.compile(r"curl|wget|https?://|npx|Invoke-WebRequest|\biwr\b|\birm\b", re.I)
SURFACES = {"cli": "CLI (terminal)", "sdk-cli": "headless / Agent SDK (claude -p)", "sdk-ts": "Agent SDK (TypeScript)",
            "sdk-py": "Agent SDK (Python)", "vscode": "IDE (VS Code)", "jetbrains": "IDE (JetBrains)",
            "desktop": "Desktop app", "web": "claude.ai web", "ide": "IDE"}


def family(model):
    m = re.match(r"claude-([a-z]+)", model or "")
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
    """100000 / '200k' / '1M' -> int tokens, or 0."""
    if isinstance(v, (int, float)):
        return int(v)
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([kKmM])?\s*$", str(v or ""))
    if not m:
        return 0
    n = float(m.group(1)) * {None: 1, "k": 1e3, "m": 1e6}[(m.group(2) or "").lower() or None]
    return int(n)


def logs_recent(days=7, limit=400):
    out = []
    since = time.time() - days * 86400
    for p in glob.glob(os.path.join(H.CLAUDE_DIR, "projects", "**", "*.jsonl"), recursive=True):
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
    surfaces, models = {}, {}
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
                        m = re.search(r'"model":\s*"(claude-[^"]+)"', line)
                        if m:
                            models[m.group(1)] = models.get(m.group(1), 0) + 1
                    if i > 300 and entry is not None and i > 3000:
                        break
        except OSError:
            continue
        if "/subagents/" not in p.replace("\\", "/"):
            surfaces[entry or "cli"] = surfaces.get(entry or "cli", 0) + 1
    return surfaces, models


def window_sources(cwd, models):
    """(window, source, settings file or None) for the real auto-compact window, in the order Claude Code
    applies them. The file is set only when autoCompactWindow in it is what applies."""
    env = parse_window(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    if env:
        return env, "CLAUDE_CODE_AUTO_COMPACT_WINDOW", None
    for p in reversed(H.settings_files(cwd)):          # local/project before user
        s = H.load_json(p)
        w = parse_window(s.get("autoCompactWindow"))
        if w:
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
    except (ValueError, TypeError, OverflowError):
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
    for p in glob.glob(os.path.join(H.CLAUDE_DIR, "projects", "*", "*.jsonl")):
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
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(after))
    return (f"flag: {n} open session(s) started before autoCompactWindow changed (at {when} Big Dad saw it {was}):"
            " the setting only protects sessions started after it is set. Restart those sessions for it to apply.")


def python_launcher():
    try:
        return open(os.path.join(BD_DIR, "python-cmd"), encoding="utf-8", errors="replace").read().strip() or "?"
    except OSError:
        return "(not cached yet: first hook run picks it)"


def short_cmd(cmd):
    """A hook command without paths or quotes: `sh run.sh context_guard.py`."""
    toks = re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', cmd or "")
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
                    except ValueError:
                        continue
                    infos = d.get("hookInfos") if isinstance(d, dict) else None
                    att = d.get("attachment") if isinstance(d, dict) else None
                    ev = "".join(w.capitalize() for w in str(d.get("subtype") or "").replace("_hook_summary", "").split("_"))
                    if isinstance(att, dict) and att.get("command"):
                        infos, ev = [att], str(att.get("hookEvent") or "")
                    for h in infos or []:
                        if not isinstance(h, dict) or not h.get("command"):
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


def measure_hooks(cwd, plugins, enabled, measure=True):
    """Every command hook per event (settings files + enabled plugins), with a measured launch cost
    for the per-tool-call events. Network-looking commands are listed but not run."""
    sfiles = H.settings_loaded(cwd)
    per_event = {}
    sh = shutil.which("sh")
    probe = json.dumps({"hook_event_name": "PostToolUse", "session_id": "bigdad-setup-probe", "tool_name": "BigDadProbe",
                        "tool_input": {}, "tool_response": "", "cwd": cwd}).encode()
    for where, cmd, root in H.hook_commands(sfiles, plugins, enabled):
        src, event = where.rsplit(" ", 1)
        row = {"from": src, "cmd": short_cmd(cmd), "ms": None}
        if measure and sh and event in PER_CALL_EVENTS + ("UserPromptSubmit",) and not NETWORK.search(cmd):
            # the same command string Claude Code hands to `sh` on every event, run once with a probe event
            full = cmd.replace("${CLAUDE_PLUGIN_ROOT}", root or "").replace("$CLAUDE_PLUGIN_ROOT", root or "")
            env = dict(os.environ, CLAUDE_PLUGIN_ROOT=root or "", BIGDAD_PROBE="1", BIGDAD_GUARD="off", BIGDAD_WATCH="off")
            t = time.perf_counter()
            try:
                subprocess.run([sh, "-c", full], input=probe, capture_output=True, env=env, timeout=5)
                row["ms"] = int((time.perf_counter() - t) * 1000)
            except (OSError, subprocess.SubprocessError):
                row["ms"] = -1
        per_event.setdefault(event, []).append(row)
    return per_event


WINDOW_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_DISABLE_1M_CONTEXT", "BIGDAD_GUARD_AT")


def fingerprint(cwd):
    """A short hash of what the profile is built from: Claude Code version, skills, plugins (version, on/off),
    MCP servers, hook commands and the window settings. Reads config files only, never a log."""
    import hashlib
    sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
    import bd_common as BC
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
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def build(cwd, measure=True):
    plugins = H.installed_plugins()
    enabled = H.enabled_plugins(cwd)
    surfaces, models = observe_logs()
    window, wsrc, wfile = window_sources(cwd, models)
    wfiles = window_files(cwd)
    wchange = window_change(load(), wfiles, wfile)
    seen_big = max(models, key=lambda m: (rank(m), models[m])) if models else ""
    big = os.environ.get("BIGDAD_BIG_MODEL") or family(seen_big) or "?"
    try:
        guard_env = int(os.environ.get("BIGDAD_GUARD_AT") or 0)
    except ValueError:
        guard_env = 0
    guard = guard_env or min(GUARD_CAP, int(window * GUARD_SHARE))
    try:
        max_big = int(os.environ.get("BIGDAD_MAX_BIG_AGENTS") or 1)
    except ValueError:
        max_big = 1
    hooks = measure_hooks(cwd, plugins, enabled, measure)
    per_call = [r for ev in PER_CALL_EVENTS for r in hooks.get(ev, [])]
    import bd_fresh
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tz": time.strftime("%Z %z"),
        "os": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
        "claude": bd_fresh.claude_version(),
        "surfaces": {SURFACES.get(k, k): v for k, v in sorted(surfaces.items(), key=lambda kv: -kv[1])},
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1])),
        "window": window, "window_source": wsrc, "window_files": wfiles, "window_change": wchange,
        "guard_at": guard, "guard_source": "BIGDAD_GUARD_AT" if guard_env else f"min({GUARD_CAP // 1000}k, {int(GUARD_SHARE * 100)}% of {window // 1000}k)",
        "big_model": big, "big_model_source": "BIGDAD_BIG_MODEL" if os.environ.get("BIGDAD_BIG_MODEL") else "priciest family seen in the logs",
        "max_big_agents": max_big,
        "skills": len(H.skill_dirs(plugins, enabled)),
        "plugins": {n.split("@")[0]: (v if enabled.get(n, False) else f"{v} (off)") for n, (_, v, _) in plugins.items()},
        "mcp": len(H.mcp_servers(cwd)),
        "mcp_in_logs": mcp_in_logs(),
        "hooks": hooks,
        "hooks_observed": hooks_observed(),
        "hooks_per_tool_call": {"count": len(per_call), "ms": sum(r["ms"] for r in per_call if (r["ms"] or 0) > 0),
                                "unmeasured": sum(1 for r in per_call if not r["ms"] or r["ms"] < 0)},
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
    except (OSError, ValueError):
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
        age = (time.time() - time.mktime(time.strptime(p.get("at", "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (ValueError, TypeError):
        return "profile has no date"
    if age > STALE_DAYS:
        return f"profile is {age:.0f} days old"
    if p.get("hooks_measured") is False:
        return "hook launch cost not measured yet"       # the startup refresh lists hooks, never runs them
    import bd_fresh
    v = bd_fresh.claude_version()
    if v and p.get("claude") and v != p.get("claude"):
        return f"Claude Code changed {p.get('claude')} -> {v}"
    return ""


def ensure(cwd=None, quiet=False):
    """Run setup when the profile is missing or stale; returns the profile."""
    why = stale()
    if why:
        if not quiet:
            print(f"  (setup: {why}; tuning Big Dad to this machine first)")
        return cmd_setup(cwd, brief=True)
    return load()


def cmd_setup(cwd=None, brief=False, measure=True):
    cwd = cwd or os.getcwd()
    p = build(cwd, measure)
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    with open(PROFILE, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)
    print(f"🥛 == SETUP  {p['at'][:16].replace('T', ' ')} {p['tz']} | {p['os']} | Claude Code {p['claude'] or '?'} | profile written (Big Dad's own knobs only)")
    print(f"  real window: {p['window'] // 1000:,}k tokens ({p['window_source']}) -> guard at {p['guard_at'] // 1000:,}k per call ({p['guard_source']})")
    print(f"  big model: {p['big_model']} ({p['big_model_source']}); default: {p['max_big_agents']} big-model background agent at a time")
    note = window_note(p)
    if note:
        print("  " + note)
    print("  surfaces seen (7 days): " + (", ".join(f"{k} x{v}" for k, v in p["surfaces"].items()) or "none"))
    print("  models seen (7 days, calls): " + (", ".join(f"{family(k)} {v}" for k, v in list(p["models"].items())[:6]) or "none"))
    hp = p["hooks_per_tool_call"]
    print(f"  setup: {p['skills']} skill folders, {len(p['plugins'])} plugins, {p['mcp']} MCP servers configured here ({p.get('mcp_in_logs', 0)} seen in logs, incl. claude.ai connectors); hooks per tool call: {hp['count']} commands,"
          f" ~{hp['ms']} ms measured launch cost" + (f" ({hp['unmeasured']} not measured)" if hp["unmeasured"] else ""))
    if not brief:
        for ev, rows in sorted(p["hooks"].items()):
            print(f"    {ev}: " + "; ".join(f"{r['from']} {r['cmd'][:28]}{'' if r['ms'] is None else f' {r['ms']} ms'}" for r in rows))
        obs = p["hooks_observed"]
        if obs:
            print(f"  hooks Claude Code logged running (7 days, every plugin incl. bundled): {len(obs)} distinct commands")
            for r in obs[:8]:
                print(f"    {r['event'] or '?':<14} {r['cmd'][:40]:<40} {r['runs']:>5} runs" + (f"  ~{r['ms']} ms" if r["ms"] else ""))
        print(f"  python launcher for hooks: {p['python_launcher']}")
    try:
        import bd_common as BC
        on = BC.stop_at()
    except Exception:
        on = 0
    if on:
        print(f"  runaway agents: warned at the guide, every call past 1.5x; hard stop ON at {on:g}x (off: bigdad.py agent-stop off)")
    else:
        print("  runaway agents: warned at the guide, every call past 1.5x. Native first: `autoCompactWindow` in a heavy"
              " helper's frontmatter (restart open sessions). Fallback, your call: `bigdad.py agent-stop 2` (a helper past"
              " 2x can only write its handoff and report back). Off by default.")
    try:
        import bd_health as H
        st = H.sentinel_status()
    except Exception:
        st = {"hook": False, "statusline": False}
    if not st["hook"]:
        print("  sentinel: off. A plugin that does not load can't warn you (a moved folder is silent). Proposal: one user-level"
              " SessionStart hook outside the plugin folder that says so. Your call: `bigdad.py sentinel on`.")
    if not st["statusline"]:
        print("  status line: off (💸 🧾 📊 each turn). Your call: `bigdad.py statusline on` (keeps yours unless --replace).")
    print("  Override: BIGDAD_GUARD_AT, BIGDAD_BIG_MODEL, BIGDAD_MAX_BIG_AGENTS. Nothing in your Claude settings was changed.")
    return p
