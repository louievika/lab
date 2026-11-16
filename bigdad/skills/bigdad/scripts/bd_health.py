"""Big Dad `health`: security and upkeep checks on the Claude Code setup. Read-only.

Security: bypass or broad allow rules, hook commands that fetch-and-run or point at missing
files, plaintext secrets in settings env (names only, never values), unpinned npx MCP servers.
Health: broken skill links, duplicate skill names, enabled-but-missing plugins, a directory
plugin whose source is newer than the installed copy, MEMORY.md over the load limit or pointing
at missing files, CLAUDE.md/AGENTS.md size and duplication, ~/.claude folder sizes, Big Dad's
state folder (where it is, whether it was refused, its Windows ACL) and the old leftovers `tidy` would
move. Reports and proposes; changes nothing.
"""
import glob, json, os, re, sys, time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
MEMORY_LINES = 200          # Claude Code loads the first 200 lines of MEMORY.md
SECRET = re.compile(r"(key|token|secret|passw|credential|auth)", re.I)
FETCH_RUN = re.compile(r"(curl|wget|iwr|Invoke-WebRequest|irm)\b.*(\|\s*(sh|bash|zsh|python\d?|node|pwsh|powershell|iex)\b|Invoke-Expression)", re.I)
BROAD = {"*", "Bash", "Bash(*)", "Bash(*:*)", "Bash(:*)", "Write", "Edit", "MultiEdit"}
# (Agent/Task are not here: starting a subagent asks no permission, and the subagent's own tools still go through
# the same rules, so an allow rule for it opens nothing new.)
FINDINGS = []


def kept_in(text):
    """The keep-list entry a finding names, or None. Security findings never go through this."""
    try:
        import bd_keep
    except ImportError:
        hooks = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")
        if hooks not in sys.path:
            sys.path.insert(0, os.path.normpath(hooks))
        try:
            import bd_keep
        except ImportError:
            return None
    return bd_keep.in_text(text)


def say(level, text, keepable=True):
    """keepable=False: privacy and security findings always show as findings, whatever the keep list says."""
    if level == "fix" and keepable and kept_in(text):   # the user chose it: show it, never count it as a cut
        FINDINGS.append("kept")
        print(f"  [kept] {re.split(r'\s*Proposal:', text)[0].rstrip()} (your choice)")
        return
    FINDINGS.append(level)
    print(f"  [{level}] {text}")


def load_json(p):
    """A dict from a JSON file; {} when it is missing, unreadable, not JSON, or not an object."""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def rel(p):
    p = p.replace("\\", "/")
    for base, tag in ((CLAUDE_DIR.replace("\\", "/"), "~/.claude"), (HOME.replace("\\", "/"), "~")):
        if p.lower().startswith(base.lower()):
            return tag + p[len(base):]
    return os.path.basename(p)


_MEMO = {}


def memo(fn):
    """One scan per process: the inventory readers below run once per distinct argument list, so
    `all` / `report` (health + setup + fresh in one process) don't rescan the same files. Callers
    must not mutate what they get back."""
    def wrap(*a):
        key = (fn.__name__, json.dumps(a, sort_keys=True, default=str))
        if key not in _MEMO:
            _MEMO[key] = fn(*a)
        return _MEMO[key]
    wrap.__name__, wrap.__doc__, wrap.__wrapped__ = fn.__name__, fn.__doc__, fn
    return wrap


def norm_path(p):
    """Claude Code keys .claude.json projects by cwd; compare with one slash style and the OS's case rule."""
    return os.path.normcase(os.path.abspath(p).replace("\\", "/").rstrip("/"))


# Managed (organisation) settings: read-only here, and they win over every other file.
# Source, checked 2026-11-14: the installed Claude Code 2.1.325 program itself (its managed-folder function and its
# managed-settings.d loader). The docs page https://code.claude.com/docs/en/settings was NOT fetched: this step had
# no network. What that program does: the folder is /Library/Application Support/ClaudeCode on macOS,
# C:\Program Files\ClaudeCode on Windows, /etc/claude-code on Linux and WSL; it reads managed-settings.json, then
# every managed-settings.d/*.json (dot-files skipped) in name order, later files winning; `allowManagedHooksOnly: true`
# there means only managed hooks (and managed plugins' hooks) run. Not read here: server-managed settings, MDM
# (macOS plist, Windows HKLM/HKCU policy registry keys) and WSL's opt-in read of the Windows policy chain.
# BIGDAD_MANAGED_DIR replaces the folder (tests point it at a temp folder, never the system one).
MANAGED_DIRS = {"darwin": "/Library/Application Support/ClaudeCode", "win32": "C:\\Program Files\\ClaudeCode"}


def managed_dir():
    return os.environ.get("BIGDAD_MANAGED_DIR") or MANAGED_DIRS.get(sys.platform, "/etc/claude-code")


def managed_files(existing=True):
    """managed-settings.json, then managed-settings.d/*.json in name order (Claude Code's order: later wins)."""
    d = managed_dir()
    main = os.path.join(d, "managed-settings.json")
    out = [main] if (not existing or os.path.isfile(main)) else []
    try:
        names = sorted(n for n in os.listdir(os.path.join(d, "managed-settings.d")) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        names = []
    return out + [p for p in (os.path.join(d, "managed-settings.d", n) for n in names) if os.path.isfile(p)]


def is_managed(p):
    d = os.path.normcase(os.path.abspath(managed_dir()))
    return os.path.normcase(os.path.abspath(p)).startswith(d + os.sep)


def where(p):
    """rel(p), but a managed file keeps its full path: it is outside ~ and the user should see which one."""
    return p.replace("\\", "/") if is_managed(p) else rel(p)


ORG = "set by your organisation (managed settings, read-only); Big Dad proposes no cut for it"


COMPACT_OFF_ENV = ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT")   # docs: either set to 1 turns it off; "off" wins


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _named(p, cwd):
    """A settings file as the user knows it: ~/.claude/... or a managed path as where(p) says, else this project's
    .claude/... by its project path."""
    if is_managed(p) or where(p).startswith("~/.claude/"):
        return where(p)
    try:
        r = os.path.relpath(p, cwd)
        if not r.startswith("..") and not os.path.isabs(r):
            return r.replace("\\", "/") + " (this project)"
    except ValueError:
        pass
    return where(p)


def auto_compact(cwd):
    """Is Claude Code's own auto-compact on here? {"on", "source", "fix"}, read the way Claude Code applies it: the
    environment (DISABLE_COMPACT / DISABLE_AUTO_COMPACT, also from a settings file's "env"), then autoCompactEnabled
    in the settings file that wins (managed, then local, project, user). Any "off" wins. Read only: Big Dad never
    changes it. fix: how to turn it back on, '' when it is on."""
    files = list(reversed(settings_loaded(cwd)))       # managed first: the first file that sets a key is the one that applies
    for k in COMPACT_OFF_ENV:
        if _truthy(os.environ.get(k)):
            return {"on": False, "source": k, "fix": f"unset {k} where it is set (your shell profile or launcher)"}
    for k in COMPACT_OFF_ENV:
        for p, s in files:
            env = s.get("env")
            if isinstance(env, dict) and k in env:
                if _truthy(env[k]):
                    src = f"{k} in the env of {_named(p, cwd)}"
                    fix = (f"{ORG}; ask your admin" if is_managed(p) else f'remove "{k}" from "env" in {_named(p, cwd)}')
                    return {"on": False, "source": src, "fix": fix}
                break
    for p, s in files:
        v = s.get("autoCompactEnabled")
        if isinstance(v, bool):
            src = f"autoCompactEnabled in {_named(p, cwd)}"
            if v:
                return {"on": True, "source": src, "fix": ""}
            fix = (f"{ORG}; ask your admin" if is_managed(p) else
                   f'set "autoCompactEnabled": true in {_named(p, cwd)} (or turn it on in /config)')
            return {"on": False, "source": src, "fix": fix}
    return {"on": True, "source": "default", "fix": ""}


def compact_off_line(ac):
    """The one plain line for setup and health when auto-compact is off; '' when it is on."""
    if not ac or ac.get("on", True):
        return ""
    what = ("auto-compact is OFF, and /compact too" if ac.get("source", "").startswith("DISABLE_COMPACT")
            else "auto-compact is OFF")
    return (f"{what} ({ac.get('source')}): Claude Code will not compact a long chat by itself, so every call keeps "
            f"re-sending all of it. To turn it on: {ac.get('fix')}. Big Dad only reads this setting.")


@memo
def managed_hooks_only(cwd):
    """True when a managed settings file sets allowManagedHooksOnly: user, project and non-managed plugin hooks
    (Big Dad's included) do not run in Claude Code then."""
    return any(s.get("allowManagedHooksOnly") is True for p, s in settings_loaded(cwd) if is_managed(p))


@memo
def settings_files(cwd, existing=True):
    """User, then project, then local, then managed (organisation) files last: later wins, managed wins over all."""
    names = ("settings.json", "settings.local.json")
    out = [os.path.join(CLAUDE_DIR, n) for n in names]
    out += [os.path.join(cwd, ".claude", n) for n in names]
    out += managed_files(existing)
    seen, uniq = set(), []
    for p in out:
        key = os.path.normcase(os.path.abspath(p))
        if (os.path.isfile(p) or not existing) and key not in seen:
            seen.add(key); uniq.append(p)
    return uniq


@memo
def settings_loaded(cwd):
    """[(path, parsed)] for every settings file that applies in cwd (user, then project, then local, then managed)."""
    return [(p, load_json(p)) for p in settings_files(cwd)]


@memo
def enabled_plugins(cwd):
    """name@market -> on/off, later settings files winning."""
    enabled = {}
    for _, s in settings_loaded(cwd):
        enabled.update(s.get("enabledPlugins") or {})
    return enabled


@memo
def marketplaces(cwd):
    """marketplace name -> source dict ({"source": "directory", "path": ...} / {"source": "github", ...}),
    from plugins/known_marketplaces.json plus settings extraKnownMarketplaces."""
    out = {}
    for n, m in (load_json(os.path.join(CLAUDE_DIR, "plugins", "known_marketplaces.json")) or {}).items():
        out[n] = (m or {}).get("source") or {}
    for _, s in settings_loaded(cwd):
        for n, m in (s.get("extraKnownMarketplaces") or {}).items():
            out[n] = (m or {}).get("source") or {}
    return out


PLUGIN_READ = {}            # name@market -> {"install": installPath, "read": where Claude Code reads it, "how": "in place" | "cache" | "dead", "dead": path}


@memo
def installed_plugins():
    """name@market -> (root, version, installedAt), root = where Claude Code actually READS the plugin:
    a "directory" marketplace is read in place (marketplace path + the plugin's `source` from its
    .claude-plugin/marketplace.json); anything else from installPath (the cache copy)."""
    data = load_json(os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")).get("plugins") or {}
    markets = marketplaces(os.getcwd())
    out = {}
    for name, entries in data.items():
        for e in (entries if isinstance(entries, list) else [entries]):
            inst = e.get("installPath") or ""
            root, how = inst, "cache"
            pname, _, mname = name.partition("@")
            src = markets.get(mname) or {}
            mdir = src.get("path") or "" if src.get("source") == "directory" else ""
            dead = ""
            if src.get("source") == "directory":
                if not mdir or not os.path.isdir(mdir):
                    dead = mdir or "(no path)"
                elif not any(os.path.isfile(os.path.join(mdir, ".claude-plugin", n)) for n in ("marketplace.json", "plugin.json")):
                    dead = os.path.join(mdir, ".claude-plugin")      # the folder is there, its manifest is not
            if dead:
                how = "dead"            # Claude Code can't load it, even when an old cache copy is still on disk
            elif mdir and os.path.isdir(mdir):
                mj = load_json(os.path.join(mdir, ".claude-plugin", "marketplace.json"))
                entry = next((p for p in mj.get("plugins") or [] if isinstance(p, dict) and p.get("name") == pname), {})
                psrc = entry.get("source")
                cand = os.path.normpath(os.path.join(mdir, psrc)) if isinstance(psrc, str) and "://" not in psrc else ""
                if cand and os.path.isdir(cand):
                    root, how = cand, "in place"
                elif not psrc and load_json(os.path.join(mdir, ".claude-plugin", "plugin.json")).get("name") == pname:
                    root, how = os.path.normpath(mdir), "in place"
            PLUGIN_READ[name] = {"install": inst, "read": root, "how": how, "dead": dead}
            out[name] = (root, e.get("version") or "?", e.get("lastUpdated") or e.get("installedAt") or "")
    return out


def stale_plugin_copies(plugins):
    """[(name@market, cache dir)] version folders under plugins/cache that are NOT where the plugin is read
    from. Reported as info only; Big Dad never deletes them."""
    out = []
    for name, (root, _, _) in plugins.items():
        pname, _, mname = name.partition("@")
        for d in sorted(glob.glob(os.path.join(glob.escape(CLAUDE_DIR), "plugins", "cache", glob.escape(mname), glob.escape(pname), "*"))):
            if os.path.isdir(d) and norm_path(d) != norm_path(root or "."):
                out.append((name, d))
    return out


def hook_commands(settings, plugins, enabled):
    """(where, command, root) for every command hook in settings files and enabled plugins."""
    out = []
    for p, s in settings:
        for event, groups in (s.get("hooks") or {}).items():
            for g in groups or []:
                for h in g.get("hooks") or []:
                    if h.get("type") == "command" and h.get("command"):
                        out.append((f"{where(p)} {event}", h["command"], None))
    for name, (root, _, _) in plugins.items():
        if not enabled.get(name, False):
            continue
        hj = load_json(os.path.join(root, "hooks", "hooks.json"))
        for event, groups in (hj.get("hooks") or {}).items():
            for g in groups or []:
                for h in g.get("hooks") or []:
                    if h.get("type") == "command" and h.get("command"):
                        out.append((f"plugin {name.split('@')[0]} {event}", h["command"], root))
    return out


def missing_targets(cmd, root):
    """Script-looking tokens in a hook command that don't exist on disk."""
    cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", root or "").replace("$CLAUDE_PLUGIN_ROOT", root or "")
    cmd = cmd.replace("$HOME", HOME).replace("~", HOME)
    miss = []
    script = re.compile(r"\.(py|sh|js|mjs|cjs|ts|ps1|cmd|bat|rb|pl)$", re.I)
    for tok in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', cmd):
        t = next(x for x in tok if x)
        if not script.search(t) or "://" in t:
            continue
        if "/" in t or "\\" in t:
            if not os.path.exists(t):
                miss.append(os.path.basename(t))
        elif root and not glob.glob(os.path.join(glob.escape(root), "**", glob.escape(t)), recursive=True):
            miss.append(t)              # bare name passed to a runner: must exist somewhere in the plugin
    return miss


def _mcp_block(d):
    """A plugin's .mcp.json may wrap servers in "mcpServers" or list them at the top level."""
    if not isinstance(d, dict):
        return {}
    if isinstance(d.get("mcpServers"), dict):
        return d["mcpServers"]
    return {k: v for k, v in d.items() if isinstance(v, dict) and (v.get("command") or v.get("url") or v.get("type"))}


@memo
def mcp_servers(cwd):
    """name -> config (+ "_source"), only what loads in THIS project: .claude.json user level, its entry for
    cwd (not other projects), ./.mcp.json, and MCP servers shipped by enabled plugins (.mcp.json or
    plugin.json "mcpServers"). With CLAUDE_CONFIG_DIR set, Claude Code keeps .claude.json in that dir."""
    out = {}

    def add(cfgs, src):
        for n, c in (cfgs or {}).items():
            c = dict(c) if isinstance(c, dict) else {"command": str(c)}
            c["_source"] = src
            out[n] = c

    cj = {}
    for p in (os.path.join(CLAUDE_DIR, ".claude.json"), os.path.join(HOME, ".claude.json")):
        if os.path.isfile(p):
            cj = load_json(p)
            break
    add(cj.get("mcpServers"), "user .claude.json")
    here = norm_path(cwd)
    for key, proj in (cj.get("projects") or {}).items():
        if norm_path(key) == here:
            add((proj or {}).get("mcpServers"), "this project in .claude.json")
    add(load_json(os.path.join(cwd, ".mcp.json")).get("mcpServers"), "./.mcp.json")
    enabled = enabled_plugins(cwd)
    for name, (root, _, _) in installed_plugins().items():
        if not enabled.get(name, False) or not root:
            continue
        pname = name.split("@")[0]
        add(_mcp_block(load_json(os.path.join(root, ".mcp.json"))), f"plugin {pname}")
        pj = load_json(os.path.join(root, ".claude-plugin", "plugin.json")).get("mcpServers")
        if isinstance(pj, str):
            pj = load_json(os.path.join(root, pj.replace("${CLAUDE_PLUGIN_ROOT}", "").lstrip("/\\")))
        add(_mcp_block(pj), f"plugin {pname}")
    return out


def dir_size(p):
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def skill_name(d):
    """The name Claude Code registers a skill under: SKILL.md frontmatter `name`, else the folder name
    (two folders with the same frontmatter name are one duplicate, whatever the folders are called)."""
    try:
        with open(os.path.join(d, "SKILL.md"), encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return os.path.basename(d)
    if head.startswith("---"):
        m = re.search(r"^name:[ \t]*[\"']?([^\"'\r\n]+?)[\"']?[ \t]*$", head.split("\n---", 1)[0], re.M)
        if m:
            return m.group(1).strip()
    return os.path.basename(d)


@memo
def skill_dirs(plugins, enabled):
    """skill name -> list of sources (user / plugin x / synced)."""
    found = {}
    for d in glob.glob(os.path.join(glob.escape(CLAUDE_DIR), "skills", "*")):
        name = os.path.basename(d)
        if name.startswith(".") or not os.path.isdir(d):
            continue
        if name == "synced":            # container for claude.ai-synced skills, one level down
            ed = glob.escape(d)
            for s in glob.glob(os.path.join(ed, "*", "*", "SKILL.md")) + glob.glob(os.path.join(ed, "*", "SKILL.md")):
                found.setdefault(skill_name(os.path.dirname(s)), []).append(("synced", os.path.dirname(s)))
            continue
        found.setdefault(skill_name(d), []).append(("user", d))
    for name, (root, _, _) in plugins.items():
        if enabled.get(name, False):
            for d in glob.glob(os.path.join(glob.escape(root), "skills", "*")):
                if os.path.isdir(d):
                    found.setdefault(skill_name(d), []).append((f"plugin {name.split('@')[0]}", d))
    for d in glob.glob(os.path.join(glob.escape(CLAUDE_DIR), "plugins", "synced", "*", "*", "skills", "*")):
        if os.path.isdir(d):
            found.setdefault(skill_name(d), []).append(("synced plugin", d))
    return found


PRIVATE_TEXT = re.compile(r"\.(md|txt|json|jsonl|csv|html|log)$", re.I)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def privacy_scan(cwd):
    """Report only: committed text files in this git repo that hold one user's private usage
    (home path, user name, e-mail, session ids), plus any audit/ folder. Nothing is changed."""
    if not os.path.isdir(os.path.join(cwd, ".git")):
        return
    try:
        import subprocess
        files = subprocess.run(["git", "ls-files"], cwd=cwd, capture_output=True, text=True, timeout=20).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return
    user = os.path.basename(HOME)
    home_marks = [HOME.replace("\\", "/"), HOME.replace("/", "\\"), f"/{user}/", f"\\{user}\\"]
    hits, audit, examples = 0, 0, {}
    for f in files:
        if not f or not PRIVATE_TEXT.search(f):
            continue
        p = os.path.join(cwd, f)
        try:
            if os.path.getsize(p) > 2_000_000:
                continue
            t = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        kinds = []
        if any(m in t for m in home_marks):
            kinds.append("home path/user name")
        if len(UUID.findall(t)) >= 3:
            kinds.append("session ids")
        if EMAIL.search(t) and "noreply" not in EMAIL.search(t).group(0):
            kinds.append("e-mail")
        if kinds:
            hits += 1
            if f.split("/")[0] == "audit":
                audit += 1
            for kd in kinds:
                examples[kd] = examples.get(kd, 0) + 1
    if hits:
        say("fix", f"privacy: {hits} committed file(s) hold one user's private usage ({', '.join(f'{k} in {n}' for k, n in examples.items())})"
                   + (f"; {audit} of them under audit/" if audit else "") + ". Before publishing: `--redact` the reports, or move them out of the repo. Report only.", keepable=False)
    elif any(f.startswith("audit/") for f in files):
        say("ok", "privacy: audit/ files are committed and look redacted (no home path, e-mail or session ids found).")


def same_path(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError, TypeError):
        return False


def running_copy():
    """Print and return the Big Dad copy answering right now (this script's own plugin folder) and the
    folder its hooks last loaded from (plugin-load.json). The recorded install can be a different copy
    (a --plugin-dir test, a moved folder), so health names both instead of guessing."""
    root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    pj = load_json(os.path.join(root, ".claude-plugin", "plugin.json"))
    if pj.get("name") != "bigdad":
        return None
    ver = str(pj.get("version") or "?")
    bd = os.environ.get("BIGDAD_HOME") or os.path.join(CLAUDE_DIR, "bigdad")
    row = load_json(os.path.join(bd, "plugin-load.json"))
    row = row if isinstance(row, dict) else {}
    line = f"  [info] Big Dad running now: v{ver} from {rel(root)}"
    lr = row.get("root")
    if isinstance(lr, str) and lr:
        if same_path(lr, root):
            line += f" (hooks last loaded from here{', ' + str(row['iso']) if row.get('iso') else ''})"
        else:
            line += (f"; hooks last loaded from a different copy, v{row.get('version') or '?'} from {rel(lr)}"
                     + (f" ({row['iso']})" if row.get("iso") else ""))
    print(line + ".")
    return {"root": root, "version": ver}


def sentinel_status():
    """{"hook": bool, "statusline": bool}: is the sentinel wired into the user's settings.json?"""
    s = load_json(os.path.join(CLAUDE_DIR, "settings.json"))
    hook = any("sentinel.py" in str(h.get("command") or "")
               for g in ((s.get("hooks") or {}).get("SessionStart") or []) if isinstance(g, dict)
               for h in (g.get("hooks") or []) if isinstance(h, dict))
    sl = s.get("statusLine") if isinstance(s.get("statusLine"), dict) else {}
    return {"hook": hook, "statusline": "sentinel.py" in str(sl.get("command") or "")}


def common():
    """hooks/bd_common (state folder, its check, the old temp place)."""
    hooks = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import bd_common
    return bd_common


ACL_NAMES = {"WD": "Everyone", "AU": "Authenticated Users", "BU": "Users", "BG": "Guests", "IU": "Interactive users",
             "NU": "Network logons", "AN": "Anonymous", "AC": "All application packages", "BO": "Backup operators",
             "PU": "Power users", "RD": "Remote desktop users", "S-1-1-0": "Everyone", "S-1-5-11": "Authenticated Users",
             "S-1-5-32-545": "Users"}


def win_acl_others(path):
    """Windows only: (state, names) for path's ACL, read by bd_common.win_sddl and win_acl_reading (states there);
    names are the trustees besides you, SYSTEM and Administrators with an allow entry (common names, else SIDs).
    ("unread", []) when it can't be read (or not Windows), and when reading it fails in any way (3.0.0 review: a
    ctypes.ArgumentError, or no ctypes at all, used to escape win_sddl and end `health` with a traceback)."""
    try:
        got = common().win_sddl(path) if os.name == "nt" else None
        if not got:
            return "unread", []
        state, who = common().win_acl_reading(*got)
    except Exception:
        return "unread", []
    names = []
    for w in who:
        if ACL_NAMES.get(w, w) not in names:          # "WD" and "S-1-1-0" are both Everyone
            names.append(ACL_NAMES.get(w, w))
    return state, names


def state_check():
    """Big Dad's state folder (2.0.9: private, no longer the shared temp folder) and the leftovers tidy would move."""
    C = common()
    p, override = C.state_path(), bool(os.environ.get("BIGDAD_STATE_DIR"))
    problem = C.state_problem(fix=False)               # 3.0.0: only looks; health makes and changes nothing
    sp, why = C.shown(p), C.shown(problem)             # thirty-seventh review: path and reason from the environment, safely
    if problem.endswith(C.TIGHTEN):
        say("info", f"state folder: {why}.")
    elif not problem and not os.path.lexists(p):
        say("ok", f"state folder: {sp} (not made yet; the hooks make it, mode 0700, when they first run)")
    elif problem:
        fix = ("point BIGDAD_STATE_DIR at a folder only you can open, or unset it" if override else
               "move what is in the way aside")
        say("sec", f"Big Dad's state folder was refused: {why}. Its hooks write nothing and say nothing until it is "
                   f"fixed (a pause you set still acts). Proposal: {fix} (default: {C.shown(os.path.join(C.bd_dir(), 'state'))}).", keepable=False)
    elif os.name != "nt":
        say("ok", f"state folder: {sp} (a real folder, yours, mode 0700)")
    else:
        acl, others = win_acl_others(p)
        what = "a real folder, not a junction or link"
        if acl == "unread":
            say("info", f"state folder: {sp} ({what}; its Windows ACL could not be read, so who else can open it is "
                        "not checked)")
        elif acl == "open":
            say("sec", f"state folder: {sp} ({what}), but it has no Windows ACL at all (a null DACL), so everyone can "
                       "open it and change what is in it. Saved command output there can hold secrets. Proposal: "
                       + ("unset BIGDAD_STATE_DIR (the default folder inherits your profile's ACL)" if override else
                          "move the folder aside; the hooks make a new one that inherits your profile's ACL") + ".",
                keepable=False)
        elif acl == "nobody":
            say("info", f"state folder: {sp} ({what}; its Windows ACL is empty, so nobody can open it, you neither: "
                        "the hooks cannot keep state there)")
        elif others:
            say("sec", f"state folder: {sp} ({what}), but its Windows ACL lets others open it: {', '.join(others)}. "
                       "Saved command output there can hold secrets. Proposal: "
                       + ("unset BIGDAD_STATE_DIR (the default folder inherits your profile's ACL)" if override else
                          "remove those entries from the folder's ACL") + ".", keepable=False)
        else:
            say("ok", f"state folder: {sp} ({what}; Windows ACL: only you, SYSTEM and Administrators)")
    try:
        import bd_tidy as T
        items = T.leftovers(fix=False)
    except Exception:
        return
    old = sum(k == "old temp" for k, _ in items)
    if items:
        say("fix", f"{len(items)} Big Dad leftover(s) older than {T.days():g} days"
            + (f", {old} of them in the old temp folder ({C.legacy_state_dir()}, from before 2.0.9)" if old else "")
            + ". `bigdad.py tidy` lists them; `tidy yes` moves them to Big Dad's trash; `tidy undo` puts them back, except old temp items, which stay in the trash until `tidy empty`.")
    else:
        say("ok", "no old Big Dad leftovers" + (" (state folder and old temp folder)" if C.legacy_state_dir() else "") + ".")


def cmd_health(cwd=None):
    cwd = cwd or os.getcwd()
    print("🥛 == HEALTH [claude] security + upkeep (read-only; proposes, never changes)")
    sfiles = settings_loaded(cwd)
    plugins = installed_plugins()
    enabled = enabled_plugins(cwd)

    # ---- managed (organisation) settings: shown, never a finding or a proposal
    for p, s in sfiles:
        if is_managed(p):
            keys = ", ".join(sorted(s)) or "nothing"
            print(f"  [org] {where(p)}: sets {keys}; {ORG}.")
    if managed_hooks_only(cwd):
        print("  [org] allowManagedHooksOnly is on, set by your organisation: Claude Code runs only managed hooks, so user, "
              "project and plugin hooks, Big Dad's guard included, do not run unless your admin manages the plugin.")
    off_line = compact_off_line(auto_compact(cwd))
    if off_line:
        print("  " + off_line)
    # ---- security
    for p, s in sfiles:
        if is_managed(p):
            continue
        perm = s.get("permissions") or {}
        if perm.get("defaultMode") in ("bypassPermissions", "dontAsk") or s.get("skipDangerousModePermissionPrompt"):
            say("sec", f"{rel(p)}: defaultMode {perm.get('defaultMode')}: every tool runs unasked. Prefer `acceptEdits` or auto mode.")
        wide = [r for r in perm.get("allow") or [] if r in BROAD or re.fullmatch(r"(?!(Agent|Task)\()\w+\(\*\)", r)]
        if wide:
            eg = []                                          # an example that fits the rules found
            if any(r.startswith("Bash") or r == "*" for r in wide):
                eg.append("`Bash(git status:*)`")
            if any(r.split("(")[0] in ("Write", "Edit", "MultiEdit") or r == "*" for r in wide):
                eg.append("`Edit(./src/**)`")
            say("sec", f"{rel(p)}: broad allow rule(s) {wide}: narrow to what you mean"
                       + (f", e.g. {' or '.join(eg)}." if eg else "."))
        env = s.get("env") or {}
        leaks = [k for k, v in env.items() if SECRET.search(k) and isinstance(v, str) and v and not v.startswith("${")]
        if leaks:
            say("sec", f"{rel(p)}: env has {len(leaks)} secret-looking value(s) in plain text ({', '.join(leaks)}). Use `apiKeyHelper` or a keychain; values not shown.")
    org_hooks = tuple(where(p) + " " for p, _ in sfiles if is_managed(p))
    for at, cmd, root in hook_commands(sfiles, plugins, enabled):
        if org_hooks and at.startswith(org_hooks):
            continue                                     # the organisation's hook: listed in setup, never a finding
        if FETCH_RUN.search(cmd):
            say("sec", f"hook {at}: fetches from the network and runs it. Pin to a local script you have read.")
        for m in missing_targets(cmd, root):
            say("fix", f"hook {at}: points at a missing file ({m}). It fails silently on every event; fix or remove it.")
    for name, cfg in mcp_servers(cwd).items():
        argv = [cfg.get("command") or ""] + [str(a) for a in cfg.get("args") or []]
        if any(os.path.basename(a).lower() in ("npx", "npx.cmd", "bunx", "pnpm", "uvx") for a in argv[:2]):
            pkgs = [a for a in argv[1:] if a and not a.startswith("-") and a != "-y"]
            if pkgs and not re.search(r"@\d|@latest|@[\^~]?\d", pkgs[0] if not pkgs[0].startswith("@") else pkgs[0][1:]):
                say("sec", f"MCP server {name}: runs `{os.path.basename(argv[0])} {pkgs[0]}` unpinned (from {cfg.get('_source', '?')}): a new upstream release runs on your machine untested. Pin a version.")
    # ---- skills
    skills = skill_dirs(plugins, enabled)
    broken = [n for n, srcs in skills.items() if any(not os.path.isfile(os.path.join(d, "SKILL.md")) for _, d in srcs)]
    for n in sorted(broken):
        say("fix", f"skill {n}: no SKILL.md behind it (broken link or empty folder). Remove it or point it at the real folder.")
    dups = {n: srcs for n, srcs in skills.items() if len(srcs) > 1}
    for n, srcs in sorted(dups.items()):
        say("fix", f"skill {n}: {len(srcs)} copies ({', '.join(s for s, _ in srcs)}). Keep one; two names in the list cost twice and confuse invocation.")
    # ---- plugins
    here = running_copy()
    for name, on in enabled.items():
        if on and name not in plugins:
            say("fix", f"plugin {name.split('@')[0]}: enabled in settings but not installed. `claude plugin install {name}` or drop the entry.")
        elif on and plugins[name][0] and not os.path.isdir(plugins[name][0]):
            say("fix", f"plugin {name.split('@')[0]}: enabled but its install folder is gone. Reinstall or disable it.")
    for name, (root, ver, when) in sorted(plugins.items()):
        info = PLUGIN_READ.get(name) or {}
        if enabled.get(name, False) and info.get("how") == "dead":
            say("fix", f"plugin {name.split('@')[0]}: its marketplace folder is gone or empty ({rel(info.get('dead') or '')}), "
                       "so Claude Code can't load it and its hooks never run (an old cache copy does not count). "
                       "If you moved it: `claude plugin marketplace add \"<new folder>\"`, then restart.")
            continue
        if not enabled.get(name, False) or not root:
            continue
        pname, how = name.split("@")[0], info.get("how")
        pj = load_json(os.path.join(root, ".claude-plugin", "plugin.json"))
        if how != "in place":
            continue                    # cache copy: version drift shows up in `fresh`
        other = " This is the recorded install, not the copy running now (see above)." if (
            pname == "bigdad" and here and not same_path(root, here["root"])) else ""
        if pj.get("version") and pj.get("version") != ver:
            say("fix", f"plugin {pname}: read in place from its source (v{pj.get('version')}), recorded install is v{ver}. "
                       f"Script edits are already live; run `claude plugin update {name}` and restart so the version and any new hooks load." + other)
        else:
            say("ok", f"plugin {pname}: read in place from its source, v{ver} (hook script edits are live in every session; new hooks.json entries need a restart)." + other)
    if any(k.split("@")[0] == "bigdad" and v for k, v in enabled.items()):
        st = sentinel_status()
        if st["hook"]:
            say("ok", "sentinel: on (a user-level SessionStart hook outside the plugin folder says so if Big Dad stops loading).")
        else:
            print("  [info] sentinel: off. If the plugin folder moves, nothing can tell you (a plugin that does not load can't speak). "
                  "Your call: `bigdad.py sentinel on` (one user-level hook, backup first; `off` undoes it).")
    stale = {}
    for name, d in stale_plugin_copies(plugins):
        stale.setdefault(name, []).append(os.path.basename(d))
    for name, vers in sorted(stale.items()):
        how = (PLUGIN_READ.get(name) or {}).get("how")
        print(f"  [info] plugin {name.split('@')[0]}: {len(vers)} stale cache cop{'y' if len(vers) == 1 else 'ies'} ({', '.join(vers)}) under "
              f"{rel(os.path.join(CLAUDE_DIR, 'plugins', 'cache'))}, not where Claude Code reads it "
              f"({'read in place from its source folder' if how == 'in place' else 'a newer install is in use'}); "
              f"safe to clear with `claude plugin update {name}` or uninstall. Big Dad never deletes them.")
    # ---- memory
    for mem in glob.glob(os.path.join(glob.escape(CLAUDE_DIR), "projects", "*", "memory", "MEMORY.md")):
        try:
            lines = open(mem, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        if len(lines) > MEMORY_LINES:
            say("fix", f"MEMORY.md ({len(lines)} lines): only the first {MEMORY_LINES} load. Move detail into topic files and keep one line each here.")
        gone = [t for t in re.findall(r"\]\(([^)#\s]+)\)", "\n".join(lines))
                if not t.startswith(("http", "mailto")) and not os.path.exists(os.path.join(os.path.dirname(mem), t))]
        if gone:
            say("fix", f"MEMORY.md: {len(gone)} link(s) point at missing files ({', '.join(gone[:5])}). Drop or fix them.")
        if len(lines) <= MEMORY_LINES and not gone:
            say("ok", f"MEMORY.md: {len(lines)} lines (limit {MEMORY_LINES}), all links resolve.")
    # ---- instruction files
    names = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")
    hits = [os.path.join(CLAUDE_DIR, n) for n in names] + [os.path.join(cwd, n) for n in names]
    d = os.path.dirname(cwd)
    while d and d != os.path.dirname(d):
        hits += [os.path.join(d, n) for n in names]
        d = os.path.dirname(d)
    texts = {}
    for p in hits:
        if os.path.isfile(p):
            try:
                texts[p] = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    tot = sum(len(t) for t in texts.values())
    if texts:
        say("ok" if tot < 12_000 else "fix", f"instruction files on this path: {len(texts)}, {tot:,} chars (≈{tot // 4:,} tokens on every call)"
            + ("" if tot < 12_000 else ": trim, or move rarely needed parts into a skill"))
    seen = {}
    for p, t in texts.items():
        for para in (x.strip() for x in t.split("\n\n")):
            if len(para) > 120:
                if para in seen and seen[para] != p:
                    say("fix", f"{os.path.basename(p)} repeats a paragraph from {os.path.basename(seen[para])} ({len(para)} chars). Keep it once.")
                    break
                seen.setdefault(para, p)
    # ---- folder sizes
    sizes = sorted(((dir_size(d), os.path.basename(d)) for d in glob.glob(os.path.join(glob.escape(CLAUDE_DIR), "*")) if os.path.isdir(d)), reverse=True)
    if sizes:
        total = sum(s for s, _ in sizes)
        print(f"  [info] ~/.claude is {total / 1e6:,.0f} MB: " + ", ".join(f"{n} {s / 1e6:,.0f} MB" for s, n in sizes[:5]))
        days_from, days = next(((p, s.get("cleanupPeriodDays")) for p, s in reversed(sfiles)       # the file that wins
                                if s.get("cleanupPeriodDays") is not None), (None, 30))
        if total > 2e9 and days_from and is_managed(days_from):
            print(f"  [org] ~/.claude over 2 GB; cleanupPeriodDays {days} in {where(days_from)}: {ORG}.")
        elif total > 2e9:
            aged = {7: 0, 14: 0, 30: 0}
            for root, _, files in os.walk(os.path.join(CLAUDE_DIR, "projects")):
                for f in files:
                    try:
                        p = os.path.join(root, f); age = (time.time() - os.path.getmtime(p)) / 86_400; sz = os.path.getsize(p)
                    except OSError:
                        continue
                    for d in aged:
                        if age > d:
                            aged[d] += sz
            say("fix", f"~/.claude over 2 GB: cleanupPeriodDays is {days} (default 30). Transcripts older than 30/14/7 days: "
                       f"{aged[30] / 1e6:,.0f}/{aged[14] / 1e6:,.0f}/{aged[7] / 1e6:,.0f} MB. Proposal: set it to 14 to reclaim ~{aged[14] / 1e6:,.0f} MB; "
                       "Big Dad's history then covers 14 days.")
    # ---- hooks per tool call (all plugins + settings); launch cost from Claude Code's logs or `setup --measure`
    try:
        import bd_setup
        prof = bd_setup.load()
    except Exception:
        prof = {}
    # thirty-ninth review: profile fields of the wrong type read as missing (a str ms or a list hooks raised here)
    BC = common()
    hp = prof.get("hooks_per_tool_call") if isinstance(prof.get("hooks_per_tool_call"), dict) else {}
    if hp:
        srcs = {}
        hooks = prof.get("hooks") if isinstance(prof.get("hooks"), dict) else {}
        for ev in bd_setup.PER_CALL_EVENTS:
            for r in hooks.get(ev) if isinstance(hooks.get(ev), list) else []:
                src = BC.label(BC.dget(r, "from"))      # fortieth review: through label, as setup prints it
                srcs[src] = srcs.get(src, 0) + 1
        # fortieth review: launch_cost_text reads ms through the same sane, so an unreadable ms is "not measured" in
        # the text as in the verdict (was [ok] beside the raw number)
        ms, cnt = BC.sane(hp.get("ms"), 0), BC.sane(hp.get("count"), 0, whole=True)
        level = "ok" if ms < 1000 and cnt <= 8 else "fix"
        say(level, f"hooks per tool call: {cnt} commands (PreToolUse/PostToolUse, matchers may skip some), "
            + bd_setup.launch_cost_text(hp) + ": " + ", ".join(f"{s} x{n}" for s, n in sorted(srcs.items(), key=lambda kv: -kv[1]))
            + ("" if level == "ok" else ". Over ~1 s per tool call adds up; drop hooks you don't use."))
    # ---- private usage committed in the working folder (before publishing)
    privacy_scan(cwd)
    # ---- big dad's state folder and leftovers (2.0.9: tidy moves them, reversibly; no delete line)
    state_check()
    n_sec, n_fix, n_kept = FINDINGS.count("sec"), FINDINGS.count("fix"), FINDINGS.count("kept")
    print(f"  {n_sec} security finding(s), {n_fix} fix(es) proposed" + (f", {n_kept} kept by you" if n_kept else "")
          + ". Each is a proposal: change only with a yes, then rerun `health`.")
