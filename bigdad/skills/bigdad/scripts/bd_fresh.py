"""Big Dad `fresh`: stay current. Report and propose only.

Claude's own knowledge stops at its training date, so Big Dad never assumes it knows what
shipped since. `fresh` states today's date, the installed vs latest Claude Code version,
every changelog / release-note line since the last check that touches context, tokens,
skills, hooks, MCP, agents, compaction or new experiments/tools/flags, and keeps the
native-feature map per surface current so Big Dad retires its own parts when Claude Code
covers them. Offline: it says so and shows the cached copy's date; it never guesses.
"""
import glob, json, os, re, subprocess, time
import bd_health as H

BD_DIR = os.path.join(H.CLAUDE_DIR, "bigdad")
INV = os.environ.get("BIGDAD_INVENTORY") or os.path.join(BD_DIR, "inventory.json")
CHANGELOG_URL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
CHANGELOG_CACHE = os.path.join(BD_DIR, "changelog.md")
RELEASE_NOTES = ("https://code.claude.com/docs/en/release-notes.md", "https://docs.claude.com/en/release-notes/claude-code.md")
TOPICS = re.compile(r"context|token|skill|hook|\bMCP\b|compact|cach(e|ing)|memory|subagent|\bagents?\b|plugin|CLAUDE\.md|transcript|re-?read|usage"
                    r"|experiment|feature flag|\bflag\b|new (tool|command|setting)|CLAUDE_CODE_[A-Z_]+", re.I)
STALE_DAYS = 3

from bd_native import RETIRE, verdict as native_verdict     # the one native-lever table (shared with versus): part, lever, surfaces, re-check words




def claude_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20, shell=os.name == "nt",
                             stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0).stdout
        m = re.search(r"\d+\.\d+\.\d+", out or "")
        return m.group(0) if m else (out or "").strip()[:20]
    except (OSError, subprocess.SubprocessError):
        return ""


def npm_latest():
    """Latest published version from npm, or '' when offline / npm missing (20 s cap)."""
    if os.environ.get("BIGDAD_OFFLINE"):
        return ""
    try:
        out = subprocess.run(["npm", "view", "@anthropic-ai/claude-code", "version"], capture_output=True, text=True,
                             timeout=20, shell=os.name == "nt").stdout
        m = re.search(r"\d+\.\d+\.\d+", out or "")
        return m.group(0) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def snapshot(cwd):
    plugins = H.installed_plugins()
    sfiles = H.settings_loaded(cwd)
    enabled = H.enabled_plugins(cwd)
    skills = sorted(H.skill_dirs(plugins, enabled))
    mcp = sorted(H.mcp_servers(cwd))
    for p in sorted(glob.glob(os.path.join(H.CLAUDE_DIR, "projects", "*", "*.jsonl")), key=os.path.getmtime, reverse=True)[:5]:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "deferred_tools_delta" in line:
                        for m in re.finditer(r"mcp__(.+?)__", line):
                            if "connector:" + m.group(1) not in mcp:
                                mcp.append("connector:" + m.group(1))
                        break
        except OSError:
            pass
    hooks = sorted(f"{where}: {cmd[:60]}" for where, cmd, _ in H.hook_commands(sfiles, plugins, enabled))
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "claude": claude_version(), "skills": skills,
            "plugins": {n: f"{v}{'' if enabled.get(n, False) else ' (off)'}" for n, (_, v, _) in plugins.items()},
            "mcp": sorted(mcp), "hooks": hooks,
            "settings": {k: v for _, s in sfiles for k, v in s.items()
                         if k in ("autoCompactWindow", "skillOverrides", "cleanupPeriodDays", "disabledMcpServers", "model")}}


def diff(old, new):
    out = []
    if old.get("claude") != new.get("claude"):
        out.append(f"claude {old.get('claude') or '?'} -> {new.get('claude') or '?'}")
    for key in ("skills", "mcp", "hooks"):
        a, b = set(old.get(key) or []), set(new.get(key) or [])
        if b - a:
            out.append(f"{key} added ({len(b - a)}): " + ", ".join(sorted(b - a)[:6]))
        if a - b:
            out.append(f"{key} removed ({len(a - b)}): " + ", ".join(sorted(a - b)[:6]))
    pa, pb = old.get("plugins") or {}, new.get("plugins") or {}
    for n in sorted(set(pa) | set(pb)):
        if pa.get(n) != pb.get(n):
            out.append(f"plugin {n.split('@')[0]}: {pa.get(n, 'none')} -> {pb.get(n, 'none')}")
    sa, sb = old.get("settings") or {}, new.get("settings") or {}
    for n in sorted(set(sa) | set(sb)):
        if sa.get(n) != sb.get(n):
            out.append(f"setting {n}: {sa.get(n, 'unset')} -> {sb.get(n, 'unset')}")
    return out


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def fetch(url, timeout=15):
    if os.environ.get("BIGDAD_OFFLINE"):
        raise OSError("offline")
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def changelog_text():
    src = os.environ.get("BIGDAD_CHANGELOG")
    if src:
        return open(src, encoding="utf-8", errors="replace").read(), "local file"
    try:
        text = fetch(CHANGELOG_URL)
        os.makedirs(BD_DIR, exist_ok=True)
        with open(CHANGELOG_CACHE, "w", encoding="utf-8") as f:
            f.write(text)
        return text, "fetched now"
    except Exception:
        try:
            when = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(CHANGELOG_CACHE)))
            return open(CHANGELOG_CACHE, encoding="utf-8", errors="replace").read(), f"OFFLINE: cached copy from {when}"
        except OSError:
            return "", "OFFLINE: no cached copy"


def changelog_top(text):
    m = re.search(r"^##\s+v?(\d+\.\d+\.\d+)", text or "", re.M)
    return m.group(1) if m else ""


def changelog_since(text, last_seen, current):
    """{version: [lines]} for versions > last_seen (or just the current one), topic lines only."""
    out, ver = {}, None
    for line in text.splitlines():
        m = re.match(r"##\s+v?(\d+\.\d+\.\d+)", line)
        if m:
            ver = m.group(1)
            keep = vtuple(ver) > vtuple(last_seen) if last_seen else ver == current
            if not keep and vtuple(ver) < vtuple(last_seen or current):
                break
            ver = ver if keep else None
            continue
        if ver and line.startswith("-") and TOPICS.search(line):
            out.setdefault(ver, []).append(line.lstrip("- ").strip())
    return out


def release_notes(known):
    """Topic lines from Anthropic's release-notes page(s) not already in the changelog lines. (reachable?, lines)"""
    if os.environ.get("BIGDAD_RELEASE_NOTES"):
        try:
            text = open(os.environ["BIGDAD_RELEASE_NOTES"], encoding="utf-8", errors="replace").read()
        except OSError:
            return False, []
    else:
        text = ""
        for url in RELEASE_NOTES:
            try:
                text = fetch(url, 10)
                break
            except Exception:
                continue
        if not text:
            return False, []
    lines = [l.lstrip("-* ").strip() for l in text.splitlines() if l.lstrip().startswith(("-", "*")) and TOPICS.search(l)]
    fresh_lines = [l for l in lines if l[:60] not in known]
    return True, fresh_lines[:10]


def last_check_age(inv):
    try:
        return (time.time() - time.mktime(time.strptime((inv.get("checked_at") or inv.get("at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (ValueError, TypeError):
        return None


def cmd_fresh(cwd=None):
    cwd = cwd or os.getcwd()
    new = snapshot(cwd)
    old = H.load_json(INV)
    today = time.strftime("%Y-%m-%d %H:%M %Z")
    age = last_check_age(old) if old else None
    print(f"🥛 == FRESH  today is {today}. Claude's own knowledge stops at its training date; everything below was read now, not remembered.")
    print(f"  last check: {'none' if age is None else f'{age:.1f} days ago'}" + ("" if age is None or age < STALE_DAYS else f" (over {STALE_DAYS} days: advice given before this check could be stale)"))
    text, how = changelog_text()
    latest_cl, latest_npm = changelog_top(text), npm_latest()
    latest = max([v for v in (latest_cl, latest_npm) if v], key=vtuple, default="")
    inst = new["claude"] or "?"
    if latest:
        state = "up to date" if vtuple(inst) >= vtuple(latest) else f"behind: {latest} is out"
        print(f"  Claude Code: installed {inst}, latest {latest} ({state}; changelog top {latest_cl or '?'}, npm {latest_npm or 'unreachable'})")
    else:
        print(f"  Claude Code: installed {inst}; latest unknown ({how}; npm unreachable). Offline: no guess made.")
    print(f"  setup now: {len(new['skills'])} skill folders on disk (yours + enabled plugins + synced), {len(new['plugins'])} plugins,"
          f" {len(new['mcp'])} MCP servers/connectors, {len(new['hooks'])} hook commands"
          + (f"; last snapshot {old.get('at', '')[:16].replace('T', ' ')}" if old else ""))
    changes = diff(old, new) if old else []
    for c in changes:
        print(f"  changed: {c}")
    if old and not changes:
        print("  no setup changes since the last snapshot")
    last = old.get("claude") if old else ""
    found = changelog_since(text, last, new["claude"]) if text else {}
    print(f"  changelog ({how}): {'versions after ' + last if last else 'current version'}; lines about context, tokens, skills, hooks, MCP, agents, compaction, experiments/flags:")
    shown, known = 0, set()
    for ver in sorted(found, key=vtuple, reverse=True):
        for line in found[ver]:
            known.add(line[:60])
            if shown < 15:
                print(f"    {ver}: {line[:150]}")
                shown += 1
    if not found:
        print("    none" if text else "    changelog not reachable and no cached copy; rerun online")
    ok, notes = release_notes(known)
    if ok:
        print(f"  release notes (Anthropic docs): reachable; {len(notes)} topic line(s) not already in the changelog" + (":" if notes else ""))
        for l in notes[:6]:
            print(f"    {l[:150]}")
    else:
        print("  release notes (Anthropic docs): not reachable now; not guessed")
    print("  native-first map (Big Dad part | native lever | surfaces with the lever | verdict):")
    for feat, lever, surfaces, words in RETIRE:
        verdict = native_verdict(words, [l for ls in found.values() for l in ls] + list(notes))
        print(f"    {feat} | {lever} | {surfaces} | {verdict}")
    print("  Surfaces: CLI, Desktop (Code tab) and IDE extensions run the local engine: hooks, settings and logs apply. Web (claude.ai/code)"
          " runs in the cloud: no local logs, Big Dad can't measure it. Chat (claude.ai, phone): no hooks, no logs, nothing to measure.")
    new["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    new["latest"] = latest
    os.makedirs(os.path.dirname(INV), exist_ok=True)
    with open(INV, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=1)
    print("  Proposals only: nothing above was changed. Say yes to any item and Big Dad applies it, proves it, measures again.")
