"""Stock Claude Code for a test run: nothing of the owner's setup, and the proof that it was.

Shared by `bigdad.py versus run` (bd_versus.py) and the receipts bench (tests/bench/bench.py, which imports it through
tests/bench/isolation.py). Lives with the skill so the shipped `versus` never imports from tests/.

How a run is isolated (all of it checked, not assumed):
  - a fresh CLAUDE_CONFIG_DIR, HOME and USERPROFILE per run, under a root OUTSIDE the user's home: Claude Code
    walks up from the working folder for CLAUDE.md / .claude/CLAUDE.md / AGENTS.md, and the owner's global
    CLAUDE.md sits at ~/.claude/CLAUDE.md, which is ".claude/CLAUDE.md" of the home folder. check_root refuses a
    root that is inside home or has any of those in an ancestor.
  - `--setting-sources ""`: no user, project or local settings files. Measured 2026-11-10 (Claude Code 2.1.319): a
    fresh config dir still downloads the account's synced plugins on its first run (ambition-lenses,
    obsidian-notes, desktop-commander, ...) and loads them from the second run on; with `--setting-sources ""`
    they are not loaded, while `--plugin-dir` (the Big Dad arm) still loads its plugin and its hooks.
  - `--strict-mcp-config` with no `--mcp-config`: no MCP servers.
  - the environment is an allow-list (ENV_KEEP: what Windows, a POSIX shell, git and node need to start, plus
    proxy and CA settings, passed as they are because a run needs them to reach the API), never the calling
    session's: no GH_TOKEN, cloud keys, ANTHROPIC_*, MCP_* or the session's own CLAUDE_*/BIGDAD_* reach a run.
    TEMP/TMP/TMPDIR point into the run's own home (the host temp holds the session's saved outputs). The run's
    own CLAUDE_CONFIG_DIR, home and Big Dad knobs are set on top (`extra`).
  - auth: only the `claudeAiOauth` block of ~/.claude/.credentials.json, WITHOUT its refreshToken (measured
    2026-11-10, Claude Code 2.1.319: a headless run works on the access token alone), is written into the temp
    config dir right before the run and deleted right after (finally, plus atexit and SIGTERM for a crash or a
    kill; sweep_logins at the next start for a hard kill), never printed. On Windows the copy gets one ACL entry,
    this user (make_private), before the token is written; if that fails the run is refused. A copy that can't
    be deleted stays listed and is reported, never counted as gone. A run is refused when the access token
    expires within REFRESH_MARGIN, so it can't run out mid-call (with no refresh token there is nothing to refresh,
    so the real login can never be rotated from a temp dir).
  - the init event of every run is checked: plugins, skills, agents and MCP servers must equal the native baseline
    (plus exactly the bigdad plugin and its skill in the Big Dad arm); no hook may run in the native arm; the
    transcript is scanned for the owner's own skill/plugin names and CLAUDE.md headings.
"""
import atexit, glob, hashlib, json, os, re, signal, subprocess, threading, time

REFRESH_MARGIN = 20 * 60                      # seconds of token life a run needs
STRIP = ("CLAUDE", "ANTHROPIC", "BIGDAD", "MCP", "AI_AGENT")   # never passed, even if named in ENV_KEEP
# the only host variables a run gets (upper-cased compare; Windows names are case-insensitive)
ENV_KEEP = frozenset((
    # Windows: system folders and the shell
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "TEMP", "TMP",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "DRIVERDATA",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "NUMBER_OF_PROCESSORS",
    "COMPUTERNAME", "USERNAME", "USERDOMAIN", "SESSIONNAME",
    # POSIX: shell, locale, terminal
    "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TERM",
    # network: proxy and certificates (needed to reach the API behind a corporate proxy)
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "ALL_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
))
ENV_KEEP_UPPER = frozenset(k.upper() for k in ENV_KEEP)
NATIVE_PLUGINS = {"cc-plugin-agents-md@builtin", "cc-plugin-telemetry@builtin", "cc-plugin-privacy@builtin",
                  "cc-plugin-plugin-authoring@builtin"}
NATIVE_AGENTS = {"claude", "Explore", "general-purpose", "Plan", "statusline-setup",
                 "web-fetch", "claude-code-guide"}   # the last two are built-ins that show up on --resume
BD_PLUGIN, BD_SKILL = "bigdad@inline", "bigdad:bigdad"
FLAGS = ("--strict-mcp-config", "--setting-sources", "")   # no MCP servers, no settings files of any scope


def real_home():
    return os.path.expanduser("~")


def check_root(root, home=None):
    """Problems with a run root (empty list = fine): inside home, or an ancestor carries instructions."""
    home = os.path.normcase(os.path.abspath(home or real_home()))
    r = os.path.normcase(os.path.abspath(root))
    bad = []
    if r == home or r.startswith(home + os.sep):
        bad.append(f"root is inside the home folder ({root}): ~/.claude/CLAUDE.md would load as project memory")
    d = os.path.abspath(root)
    while True:
        for rel in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", os.path.join(".claude", "CLAUDE.md"),
                    os.path.join(".claude", "settings.json"), os.path.join(".claude", "skills")):
            if os.path.exists(os.path.join(d, rel)):
                bad.append(f"ancestor carries {os.path.join(d, rel)}")
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return bad


def run_env(cfg, home, state, extra=None):
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP_UPPER and not k.upper().startswith(STRIP)}
    env.update(CLAUDE_CONFIG_DIR=cfg, HOME=home, USERPROFILE=home, BIGDAD_STATE_DIR=state, DISABLE_AUTOUPDATER="1",
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    # every other name Windows tools use to find the profile points into the temp home too (Grok review): a shell
    # step can't reach the real ~/.claude through $env:HOMEDRIVE$env:HOMEPATH or %APPDATA%. PATH stays real
    # (python, git, the claude binary itself).
    full = os.path.abspath(home)
    drive, rest = os.path.splitdrive(full)
    env.update(HOMEDRIVE=drive, HOMEPATH=rest or os.sep,
               APPDATA=os.path.join(full, "AppData", "Roaming"), LOCALAPPDATA=os.path.join(full, "AppData", "Local"))
    if os.path.isdir(full):              # the run's temp is its own too (Grok review): the host temp holds the
        tmp = os.path.join(full, "tmp")  # calling session's saved outputs, which a run's shell could read
        try:
            os.makedirs(tmp, exist_ok=True)
            env.update(TEMP=tmp, TMP=tmp, TMPDIR=tmp)
        except OSError:
            pass
    for k in ("APPDATA", "LOCALAPPDATA"):
        if os.path.isdir(full):          # only inside a run's own home (never a stray folder for a bare name)
            try:
                os.makedirs(env[k], exist_ok=True)
            except OSError:
                pass
    env.update(extra or {})
    return env


def auth_block(src=None):
    """The claudeAiOauth block only (no MCP tokens, no refreshToken), or raise when it is missing or about to
    expire."""
    src = src or os.path.join(real_home(), ".claude", ".credentials.json")
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    block = d.get("claudeAiOauth") if isinstance(d, dict) else None
    if not isinstance(block, dict) or not block.get("accessToken"):
        raise RuntimeError("no claudeAiOauth login in ~/.claude/.credentials.json")
    left = (block.get("expiresAt") or 0) / 1000 - time.time()
    if left < REFRESH_MARGIN:
        raise RuntimeError(f"login token expires in {left / 60:.0f} min; open Claude Code once so it refreshes, then rerun")
    return {"claudeAiOauth": {k: v for k, v in block.items() if k != "refreshToken"}}


def _sys32(exe):
    return os.path.join(os.environ.get("SYSTEMROOT") or "C:" + os.sep + "Windows", "System32", exe)


_SID = []


def _my_sid():
    if not _SID:
        out = subprocess.run([_sys32("whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             timeout=30).stdout
        m = re.search(r'"(S-1-[0-9-]+)"', out or "")
        if not m:
            raise RuntimeError("could not read this user's SID (whoami /user)")
        _SID.append(m.group(1))
    return _SID[0]


def make_private(path, folder=False):
    """Windows: one entry left on path, this user with full control, nothing inherited (Grok review: 0o600 means
    nothing there, and a file under the C: drive root inherits "Users: read"). Raises when that can't be set. No-op
    on POSIX, where the 0o600 / 0o700 mode does the job."""
    if os.name != "nt":
        return
    grant = "*" + _my_sid() + ":" + ("(OI)(CI)F" if folder else "F")
    r = subprocess.run([_sys32("icacls.exe"), path, "/inheritance:r", "/grant:r", grant], capture_output=True,
                       text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"could not make {os.path.basename(path)} private (icacls rc {r.returncode})")


_LIVE = set()                                 # login copies on disk right now (deleted at exit, whatever the exit)
_LIVE_LOCK = threading.Lock()


def _drop_live():
    with _LIVE_LOCK:
        paths = list(_LIVE)
    for p in paths:
        _remove_login(p)


def _remove_login(p):
    """Delete one login copy; it stays on the live list until it is really gone (Grok review), so a later
    clean-up tries again."""
    try:
        os.remove(p)
    except OSError:
        pass
    gone = not os.path.exists(p)
    if gone:
        with _LIVE_LOCK:
            _LIVE.discard(p)
    return gone


atexit.register(_drop_live)


def _on_term(signum, frame):
    _drop_live()
    raise SystemExit(128 + signum)


def _hook_term():
    """SIGTERM ends the process through SystemExit (so finally and atexit run), only if nobody else set a handler."""
    sig = getattr(signal, "SIGTERM", None)
    if sig is None or threading.current_thread() is not threading.main_thread():
        return
    try:
        if signal.getsignal(sig) in (signal.SIG_DFL, None):
            signal.signal(sig, _on_term)
    except (OSError, ValueError):
        pass


def sweep_logins(root):
    """Delete every login copy under a run root (a run killed hard last time); every copy it found, deleted or
    not. logins_stuck(found) is the ones still on disk."""
    found = []
    if not root or not os.path.isdir(root):
        return found
    for r, _, files in os.walk(root):
        if ".credentials.json" in files:
            p = os.path.join(r, ".credentials.json")
            found.append(p)
            _remove_login(p)
    return found


def logins_stuck(paths):
    """The login copies of paths that could not be deleted (still on disk)."""
    return [p for p in paths if os.path.exists(p)]


class Auth:
    """with Auth(cfg): the auth file exists in cfg only inside the block (and is removed at exit if the block never
    got to its end)."""
    def __init__(self, cfg, block):
        self.path = os.path.join(cfg, ".credentials.json")
        o = block.get("claudeAiOauth") if isinstance(block, dict) else None
        self.block = {"claudeAiOauth": {k: v for k, v in o.items() if k != "refreshToken"}} if isinstance(o, dict) else block

    def __enter__(self):
        exp = (self.block.get("claudeAiOauth") or {}).get("expiresAt") if isinstance(self.block, dict) else None
        if isinstance(exp, (int, float)) and exp / 1000 - time.time() < REFRESH_MARGIN:
            self.block = auth_block()         # re-read the real login (it may have refreshed); raises if still short
        _hook_term()
        with _LIVE_LOCK:
            _LIVE.add(self.path)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            make_private(self.path)           # before one byte of the token is in it
        except Exception:
            os.close(fd)
            _remove_login(self.path)
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.block, f)
        return self

    def __exit__(self, *a):
        _remove_login(self.path)


def markers(home=None):
    """Names from the owner's real setup that must never show up in a bench run (skills, plugins, agents, CLAUDE.md
    headings). Generic words that collide with built-in skills are skipped."""
    home = home or real_home()
    c = os.path.join(home, ".claude")
    names = set()
    for pat in ("skills/*", "skills-off/*", "agents/*.md", "plugins/cache/*/*", "plugins/synced/*/*.meta.json"):
        for p in glob.glob(os.path.join(c, pat)):
            n = os.path.basename(p)
            for suf in (".meta.json", ".md"):
                n = n[: -len(suf)] if n.endswith(suf) else n
            names.add(n)
    heads = []
    try:
        with open(os.path.join(c, "CLAUDE.md"), encoding="utf-8", errors="replace") as f:
            heads = [l.strip()[3:].strip() for l in f if l.startswith("## ")]
    except OSError:
        pass
    names = {n for n in names if len(n) >= 6 and n != "bigdad" and not re.fullmatch(r"[0-9a-f-]{20,}", n)}
    return sorted(names), [h for h in heads if len(h) >= 8]


def check_init(init, arm_has_plugin, baseline_skills):
    """Isolation problems in one run's init event (empty list = clean)."""
    bad = []
    if not isinstance(init, dict):
        return ["no init event"]
    plugins = {p.get("source") for p in init.get("plugins") or [] if isinstance(p, dict)}
    want = NATIVE_PLUGINS | ({BD_PLUGIN} if arm_has_plugin else set())
    if plugins != want:
        bad.append(f"plugins {sorted(plugins ^ want)} differ from native{' + bigdad' if arm_has_plugin else ''}")
    if init.get("mcp_servers"):
        bad.append(f"MCP servers loaded: {[m.get('name') for m in init['mcp_servers'] if isinstance(m, dict)]}")
    skills = set(init.get("skills") or [])
    want_s = set(baseline_skills) | ({BD_SKILL} if arm_has_plugin else set())
    if skills != want_s:
        bad.append(f"skills {sorted(skills ^ want_s)} differ from the native baseline")
    agents = set(init.get("agents") or [])
    if agents - NATIVE_AGENTS:
        bad.append(f"extra agents {sorted(agents - NATIVE_AGENTS)}")
    if init.get("output_style") not in (None, "default"):
        bad.append(f"output style {init.get('output_style')}")
    return bad


def scan_leaks(texts, names, heads):
    """Owner-setup names found in the given texts (skill listings, transcript, init)."""
    hits = set()
    for t in texts:
        for n in names:
            if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", t):
                hits.add(n)
        for h in heads:
            if h in t:
                hits.add("CLAUDE.md: " + h)
    return sorted(hits)


def tree_hash(d):
    """{relative path: sha256} of a folder (the real ~/.claude/bigdad)."""
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            try:
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, d)] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                pass
    return out


def tree_diff(before, after, d, needles):
    """Changed files and whether any of them mentions the bench (a bench session id or the bench root)."""
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    ours = []
    for rel in changed:
        try:
            with open(os.path.join(d, rel), "rb") as f:
                blob = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if any(n and n in blob for n in needles):
            ours.append(rel)
    return changed, ours
