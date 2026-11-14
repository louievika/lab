#!/usr/bin/env python3
"""Big Dad output shrink: big, noisy Bash/PowerShell output is cut before it enters the chat (PostToolUse).

Mechanism (native, checked live on Claude Code 2.1.319): PostToolUse may return `updatedToolOutput`, which
replaces what Claude sees for a built-in tool when it has the tool's own output shape. The command itself is
never touched: it already ran, untouched, with its own exit code, side effects and permission check. Only the
text the model reads is shorter. (squeez and rtk rewrite the command in PreToolUse instead; that changes what the
permission rules see and has broken exit codes and Windows quoting for them. Big Dad doesn't rewrite.)

  - Only successful calls arrive here: a failing command goes to PostToolUseFailure, which Big Dad leaves alone,
    so a failed test run or build is always shown as Claude Code shows it.
  - Only known noisy families, and every part of the command must be one of them (or cd/export/echo...):
    test runners, git log/status/diff/show and network chatter, package installs, builds and linters,
    find/ls -R/tree, docker/kubectl/journalctl logs. Anything else, cat/head/tail of a file (the read guard
    needs those whole), pipes (already filtered), redirects, heredocs, sudo, watch/follow modes and
    machine-readable output (--json, -json, -o json, --porcelain, -z, --format=...), find -exec/-print0, fd -x,
    git show REV:PATH (a file), git log with patches (-p/-u/-c/--cc/-m) or --raw, output that IS the file or
    a diff (prettier/biome format/esbuild/swc to stdout, --diff, --stdout), lists and plans (make help, rake -T,
    jest --listTests, --collect-only) and saved outputs over 4 MB are left as they are. The full copy is private
    (per-user 0700 folder, 0600 file on macOS/Linux, where the temp dir is shared; no symlink/junction).
  - Kept: error/failure/warning lines with what follows them (tracebacks), file:line references, every failure
    section of a test run, the first and last lines, and a one-line note with the full output's path. When the
    error lines alone are over the budget, the note says how many did not fit.
  - Cut: passing-test lines, progress bars, download/compile chatter, git hints, runs of near-identical lines,
    and over-long lines. Tiny outputs, binary output and cuts that save under 30% / 250 tokens are left whole.
  - Full output: saved to a temp file first. Output over ~30k chars is left to Claude Code, which saves it and
    shows a 2KB preview on its own (2.0.0: the old branch that re-cut that preview saved ~100 tokens a time).
    Nothing is lost; the note says where it is. If saving fails, nothing is cut.
  - Receipts: the note carries tokens before -> after (~4 chars per token, like every Big Dad estimate); the
    chat receipt adds them up from the chat's own log, and ~/.claude/bigdad/shrinks.jsonl gets one row each.
  - Cap (2.0.5): output of a command no family knows, over 4,000 chars, keeps its start and its end; the note
    sits where the middle was and names the full copy. File reads are exempt (every part of the command starts
    with cat/head/tail/sed/grep/rg/awk/Get-Content..., or git show REV:PATH): the model asked for that text.
    So is output already cut by a final `| tail -N` / `| head -N` (N up to 200): the caller chose that cut.
    2.0.9: only when that pipeline is all that prints (cd/export aside) and the real output agrees: no more
    lines than N, normal-width lines (400 chars on average, none over 4,000). Else the cap applies.
  - Any internal error: no output, so Claude sees the original.

  BIGDAD_SHRINK=off          turn it off (or `bigdad.py shrink off`, which writes ~/.claude/bigdad/shrink)
"""
import json, os, re, stat, sys, time, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

MARK = "Big Dad shrink"
SHELLS = ("Bash", "PowerShell")
MIN_CHARS = 2_000          # smaller outputs are left whole (~500 tokens)
MIN_LINES = 30
MIN_SAVE_TOKENS = 250      # net win gate: a cut must save this many tokens...
MIN_SAVE_SHARE = 0.30      # ...and this share of the output, after counting its own note
INLINE_CAP = 30_000        # Claude Code keeps a Bash output inline up to ~30k chars, else a file + 2k preview
PREVIEW_CHARS = 2_000
BUDGET = 12_000            # most characters a shrunk view may have (inline)
LINE_CAP = 400             # characters kept of one line
IMPORTANT_CAP = 250        # error/warning lines kept (first half + last half past this; the note counts the rest)
FAIL_CAP = 400             # lines kept of the failure sections of a test run
CAP_CHARS = 4_000          # 2.0.5: output of a command no family knows (and not a file read) is capped here
CAP_TAIL = 900             # ...of which the end; the note follows the start, so the receipts (last 1,500 chars) see it

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
IMPORTANT = re.compile(
    r"(?i)(\b(errors?|err!|fail|failed|failures?|failing|fatal|panic(ked)?|exceptions?|traceback|warn(ings?)?|denied|refused|"
    r"cannot|can't|couldn't|unable|not found|no such|invalid|undefined|unresolved|segmentation fault|abort(ed)?|"
    r"timed? ?out|conflicts?|deprecat\w*|vulnerab\w*|critical|severe|assert\w*|mismatch|missing|xfail\w*|xpass\w*|"
    r"broken|rejected|unauthori[sz]ed|forbidden|killed|oom|unmerged|both (modified|added|deleted)|"
    r"(deleted|added) by (us|them))\b"
    r"|^E\s{2,}|^\s*File \".+\", line \d+|^\s+at \S.*[:(]\d+|^\s*--> \S+:\d+|"
    r"(^|[\s(\[\"'])(?:[A-Za-z]:)?[\w.~/\\-]*\.[A-Za-z]\w{0,7}[:(]\d+)")
NOT_IMPORTANT = re.compile(
    r"(?i)^\s*(\d+ passed|ok\b|0 (errors?|failures?|failed|warnings?)\b|.*\b(errors?|failures?|failed)[:=]\s*0\b(?!\.\d))")
FAIL_HEAD = re.compile(
    r"^=+ (FAILURES|ERRORS|short test summary info) =+$|^(FAIL|ERROR): \S|^_{3,} .+ _{3,}$|^failures:$|^--- FAIL:|"
    r"^\s*● |^\s+\d+\) \S|^Failed tests:|^Tests in error:|^\s*FAIL\s+\S|^\s*×\s|^\s*✕\s|^\s*✗\s")
# family-specific noise: lines that carry no news once the run passed
NOISE = {
    "test": [r"^\S.* \.\.\. (ok|skipped.*|expected failure)$", r"^\S+(\.py)?::\S+ (PASSED|SKIPPED|XFAIL)\b",
             r"^\S+\.py [.s]+\s*(\[\s*\d+%\])?$", r"^[.s]{3,}$", r"^(collecting|collected) ", r"^(platform|cachedir|rootdir|"
             r"configfile|plugins|testpaths): ", r"^\s*(✓|✔|√|PASS\b|○ skipped)", r"^test \S+ \.\.\. (ok|ignored)$",
             r"^=== (RUN|PAUSE|CONT)\b", r"^\s*--- (PASS|SKIP):", r"^ok\s+\S+\s+(\(cached\)|[\d.]+s)", r"^\s*Passed \S",
             r"^\[INFO\] Tests run: \d+, Failures: 0, Errors: 0", r"^\s*Running \S", r"^\s*Compiling \S", r"^\s*Doc-tests "],
    "install": [r"^\s*(Collecting|Downloading|Using cached|Obtaining|Stored in directory|Created wheel|Building wheels? "
                r"for|Preparing metadata|Getting requirements|Installing build dependencies|Attempting uninstall|Found "
                r"existing installation|Uninstalling|Successfully uninstalled|Requirement already satisfied|Looking in "
                r"indexes|Processing )", r"^npm (http|timing|sill|verb|info) ", r"^\s*(Downloaded|Downloading|Compiling|"
                r"Fresh|Checking|Updating|Locking|Adding|Blocking|Unpacking|Fetching|Resolving|Linking|Progress)\b",
                r"^go: (downloading|finding|extracting) ", r"^\[\d+/\d+\] ", r"^(Get|Hit|Ign):\d+ ", r"^\s*[+~] \S+==\S+$",
                r"^(Reading|Building) (package lists|dependency tree|state information)"],
    "build": [r"^\s*(Compiling|Checking|Fresh|Downloaded|Downloading|Building|Linking|Generating|Scanning) \S",
              r"^\[\s*\d+%\]", r"^\s*(CC|CXX|LD|AR|AS|CCLD|CXXLD|GEN)\s+\S", r"^make\[\d+\]: (Entering|Leaving) directory",
              r"^\s*(gcc|g\+\+|clang|clang\+\+|cc|c\+\+|cl\.exe|cl)\s+-", r"^\s*(asset|chunk|cached modules|orphan modules|"
              r"runtime modules|modules by path) .*\[(built|emitted|cached|code generated)\]", r"^\s*dist/\S+\s+[\d.,]+ k?B\b",
              r"^#\d+ (sha256|extracting|\[internal\]|resolve|transferring|DONE|CACHED)\b", r"^\s*Determining projects to restore",
              r"^\s*Restored \S"],
    "git": [r"^\s*\(use \"git .*\"( to .*)?\)$", r"^\s*\(commit or discard .*\)$", r"^\s*\(use \"git .*$",
            r"^remote: (Counting|Compressing|Enumerating|Total|Finding sources)\b", r"^(Receiving|Resolving|Counting|"
            r"Compressing|Enumerating|Writing|Unpacking|Updating files|Checking out files|Filtering content)\b.*(\d+%|done)"],
}
NOISE_RE = {k: re.compile("|".join(f"(?:{p})" for p in v)) for k, v in NOISE.items()}
PROGRESS = re.compile(r"^\s*[\[(]?\s*\d{1,3}(\.\d+)?\s?%|[█▓▒░■□▏▎▍▌▋▊▉]{3,}|\[[#=>\-. ]{6,}\]|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]\s")
SIMILAR = re.compile(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{7,}|\d+")

# --- what a command is ---------------------------------------------------------------------------------------------
NEUTRAL = {"cd", "pushd", "popd", "export", "set", "source", ".", "echo", "printf", "true", ":", "mkdir", "activate",
           "set-location", "sl", "write-host", "write-output", "unset", "setlocal", "nvm", "pyenv"}
WRAPPERS = {"time", "nice", "command", "builtin", "noglob", "stdbuf", "env", "xvfb-run", "caffeinate", "nohup"}
RUNNERS = {("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("pdm", "run"), ("hatch", "run"), ("bundle", "exec"),
           ("conda", "run"), ("rye", "run"), ("pixi", "run")}
SKIP_FLAGS = re.compile(
    r"^(-f|--follow|-F|--watch|--watchall|--watch-all|-w|--interactive|-i|-p|--patch|--json|--porcelain(=\S*)?|-z|--null|"
    r"--name-only|--name-status|--format(=.*)?|--pretty=((format|tformat):|%).*|--reporter=json|--json[-=]\S*|-ojson|"
    r"--output=json|--template=.*|--raw|--numstat|--shortstat|--stat|--dirstat.*|--help|-h|--version|-V|--dry-run|"
    r"--diff|--stdout|--listTests|--list-tests|--collect-only|--co|--showConfig|--show-config|--print-config)$")
FORMAT_FLAG = re.compile(r"(?i)^-[-\w]*(=|:)?.*\b(nd)?jsonl?\b|^-[-\w]*(format|output|reporter)[-\w]*=(xml|sarif|junit\w*|csv|ya?ml|tap)\b|"
                         r"^-json$|^--report(=.*)?$")
FORMAT_WORDS = {"json", "ndjson", "jsonl", "json-lines", "json-pretty", "json-seq", "json-sse", "xml", "sarif", "junit",
                "junitxml", "csv", "yaml", "yml", "tap"}
LOG_PATCH = re.compile(r"^(-u|-c|--cc|-m|--patch-with-stat|--patch-with-raw|-U\d*|--unified(=\d+)?|--word-diff(=\S*)?|"
                       r"--color-words(=\S*)?|--full-diff|--remerge-diff|--diff-merges(=\S*)?|-L\S*)$")
TASK_LIST = {"-T", "--tasks", "-l", "--list", "-n", "--just-print", "--recon", "--summary", "--show", "--dump", "--evaluate",
             "--variables", "--choose", "-P", "--prereqs", "-W", "--where", "-D", "--describe", "--list-all"}
BUILD_TARGETS = re.compile(r"^(all|build|compile|install|dist|release|debug|lint|format|fmt|clean|deps|package|bundle|"
                           r"assets|vendor|generate|gen|default|docker|image|setup|bootstrap)([-_:.]\w+)*$")
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-printf", "-fprintf", "-print0", "-fprint", "-fprint0", "-fls", "-delete"}
FD_ACTIONS = {"-x", "--exec", "-X", "--exec-batch", "-0", "--print0"}
TEST_RUNNERS = {"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "rspec", "phpunit", "ctest", "ava",
                "karma", "playwright", "cypress", "nextest", "behave", "nose2", "ward", "deno"}
LINTERS = {"eslint", "ruff", "flake8", "pylint", "mypy", "pyright", "tsc", "tslint", "stylelint", "rubocop", "shellcheck",
           "golangci-lint", "black", "isort", "prettier", "biome", "clippy", "webpack", "vite", "rollup", "esbuild",
           "ninja", "cmake", "msbuild", "meson", "bazel", "scons", "next", "nuxt", "turbo", "nx", "parcel", "swc", "javac"}
INSTALLERS = {"pip", "pip3", "pipx", "uv", "poetry", "pdm", "conda", "mamba", "micromamba", "gem", "bundle", "composer",
              "brew", "choco", "scoop", "winget", "apt", "apt-get", "dnf", "yum", "pacman", "apk", "zypper", "port"}
JS_PM = {"npm", "yarn", "pnpm", "bun"}
INSTALL_SUBS = {"install", "i", "ci", "add", "update", "upgrade", "sync", "lock", "restore", "remove", "uninstall",
                "fetch", "download", "tidy", "outdated", "audit", "reinstall", "up", "dedupe", "prune", "bootstrap"}


def _toks(seg):
    return re.findall(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+", seg)


def _unq(t):
    return t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t


def _bare(t):
    """A program token as a name: path and .exe/.cmd/.bat/.ps1 dropped, lower case (./gradlew -> gradlew)."""
    t = _unq(t).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return re.sub(r"\.(exe|cmd|bat|ps1|sh)$", "", t)


def strip_prefix(toks):
    """The command a segment really runs: env assignments, time/nice/timeout, uv run/npx/python -m dropped."""
    t = list(toks)
    for _ in range(8):
        if not t:
            return t
        b = _bare(t[0])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t[0]) or re.match(r"^\$env:", t[0], re.I):
            t = t[1:]
        elif b in WRAPPERS:
            t = t[1:]
            while t and (t[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t[0])):
                t = t[1:]
        elif b == "timeout" and len(t) > 2:
            t = t[1:]
            while t and (t[0].startswith("-") or re.match(r"^\d+(\.\d+)?[smhd]?$", t[0])):
                t = t[1:]
        elif len(t) > 1 and (b, _bare(t[1])) in RUNNERS:
            t = t[2:]
        elif b in ("npx", "bunx", "pnpx") or (b in ("pnpm", "yarn", "bun") and len(t) > 1 and t[1] in ("exec", "dlx", "x")):
            t = t[2:] if b in ("pnpm", "yarn", "bun") else t[1:]
            while t and t[0].startswith("-"):
                t = t[1:]
        elif re.match(r"^(python|python3|py|pypy3?)(\d+(\.\d+)*)?$", b):
            rest = t[1:]
            while rest and re.match(r"^-(\d(\.\d+)?|[BOuEsIq]+|W\S*|X\S*)$", rest[0]):
                rest = rest[1:]
            if len(rest) >= 2 and rest[0] == "-m":
                t = rest[1:]
            elif rest and re.search(r"(^|[/\\])manage\.py$", _unq(rest[0])) and len(rest) > 1 and rest[1] == "test":
                return ["pytest"] + rest[2:]
            else:
                return t
        else:
            return t
    return t


def _sub(t, start=1):
    """First non-option word after the program (git -C dir -c k=v log -> log)."""
    i = start
    while i < len(t):
        w = t[i]
        if w in ("-C", "-c", "--git-dir", "--work-tree", "--prefix", "--cwd", "--dir", "-w", "--workspace", "--filter"):
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        return _unq(w).lower(), i
    return "", i


def family_of(toks):
    """'test' / 'install' / 'build' / 'git' / 'gitlog' / 'gitdiff' / 'gitstatus' / 'list' / 'log', or None."""
    t = strip_prefix(toks)
    if not t:
        return None
    v = _bare(t[0])
    sub, i = _sub(t)
    if v == "git":
        if sub in ("log", "shortlog", "reflog"):
            if any(LOG_PATCH.match(_unq(w)) for w in t[i + 1:]):
                return None                    # -u, -c, --cc, -m, -U3, -L...: a log with patches, like -p
            return "gitlog"
        if sub == "whatchanged":
            return None                        # git log --raw: left whole, like --raw
        if sub == "status":
            return "gitstatus"
        if sub == "show" and any(":" in _unq(w) and not _unq(w).startswith("-") for w in t[i + 1:]):
            return None                        # git show REV:PATH prints a file: the model needs it whole
        if sub in ("diff", "show"):
            return "gitdiff"
        if sub in ("fetch", "pull", "push", "clone", "checkout", "switch", "merge", "rebase", "cherry-pick", "gc",
                   "submodule", "lfs", "branch", "tag", "stash", "worktree", "remote", "restore", "revert", "am", "prune"):
            return "git"
        return "list" if sub == "ls-files" else None
    if v in TEST_RUNNERS or v in ("unittest", "nose", "green"):
        return "test" if v != "deno" or sub == "test" else None
    if v == "cargo":
        return {"test": "test", "nextest": "test", "bench": "test", "build": "build", "check": "build", "clippy": "build",
                "doc": "build", "fetch": "install", "install": "install", "update": "install", "fmt": "build"}.get(sub)
    if v == "go":
        return {"test": "test", "build": "build", "vet": "build", "install": "build", "generate": "build",
                "mod": "install", "get": "install"}.get(sub)
    if v == "dotnet":
        return {"test": "test", "build": "build", "publish": "build", "pack": "build", "restore": "install"}.get(sub)
    if v in ("mvn", "mvnw", "gradle", "gradlew", "sbt", "lein", "ant"):
        words = [_unq(w).lower() for w in t[1:] if not w.startswith("-")]
        return "test" if any(w in ("test", "verify", "check", "integrationtest") or w.endswith(":test") for w in words) \
            else ("build" if words else None)
    if v in ("make", "gmake", "nmake", "mingw32-make", "just", "task", "rake", "invoke"):
        if any(_unq(w) in TASK_LIST for w in t[1:]):
            return None                        # make -n, rake -T, just --list: the output is the plan or the list
        if re.search(r"test|check|spec", sub):
            return "test"
        return "build" if not sub or BUILD_TARGETS.match(sub) else None   # make help, rake routes: a table, left whole
    if v == "prettier":
        return "build" if any(_unq(w) in ("--write", "--check", "-c", "--list-different", "-l") for w in t[1:]) else None
    if v == "biome":
        return "build" if sub in ("check", "lint", "ci") or "--write" in t else None   # biome format prints the file
    if v == "esbuild":
        return "build" if any(_unq(w).startswith(("--outfile", "--outdir")) for w in t[1:]) else None
    if v == "swc":
        return "build" if any(_unq(w).split("=")[0] in ("-o", "--out-file", "-d", "--out-dir") for w in t[1:]) else None
    if v in JS_PM:
        if v in ("yarn", "pnpm", "bun") and not sub:
            return "install"
        if sub in INSTALL_SUBS:
            return "install"
        if sub in ("test", "t", "tst"):
            return "test"
        if sub in ("run", "run-script"):
            sub = _sub(t, i + 1)[0]
        if re.search(r"(^|:)(test|spec|e2e|check)", sub):
            return "test"
        if re.search(r"(^|:)(build|compile|lint|typecheck|tsc|bundle|dist)", sub):
            return "build"
        return None
    if v in INSTALLERS:
        if v == "uv" and sub == "pip":
            sub = _sub(t, i + 1)[0]
        if v in ("brew", "apt", "apt-get") and sub in ("list", "search", "info", "show"):
            return None
        return "install" if sub in INSTALL_SUBS else None
    if v in LINTERS:
        return "build"
    if v in ("docker", "podman", "docker-compose"):
        if sub == "compose":
            sub = _sub(t, i + 1)[0]
        if sub in ("build", "pull", "push"):
            return "build" if sub == "build" else "install"
        return "log" if sub == "logs" else None
    if v in ("kubectl", "oc") and sub == "logs":
        return "log"
    if v in ("journalctl",):
        mode = ""
        for k, w in enumerate(t[1:], 1):
            w = _unq(w)
            if (w == "--output" or re.match(r"^-[A-Za-z]*o$", w)) and k + 1 < len(t):
                mode = _unq(t[k + 1])
            elif w.startswith("--output="):
                mode = w[9:]
            elif re.match(r"^-[A-Za-z]*o\S", w) and not w.startswith("--"):
                mode = w[w.index("o") + 1:]
        # short*, cat, with-unit are text; json*, export, verbose are records to parse
        return "log" if not mode or mode.startswith("short") or mode in ("cat", "with-unit") else None
    if v in ("find", "fd", "fdfind", "tree", "get-childitem", "gci", "dir", "ls"):
        words = {_unq(w) for w in t[1:]}
        if (v == "find" and words & FIND_ACTIONS) or (v in ("fd", "fdfind") and words & FD_ACTIONS) or \
                (v == "tree" and (words & {"--json", "--xml"} or any(re.match(r"^-[A-Za-z]*[JXH]", w) for w in words))):
            return None                        # the output is another command's, or a format to parse (tree -J/-X/-H html)
        if v in ("ls", "dir", "get-childitem", "gci"):
            flags = " ".join(w for w in t[1:] if w.startswith("-") or w.startswith("/"))
            if not re.search(r"(^|\s)-[A-Za-z]*R|--recursive|(?i:-recurse\b|/s\b)", flags):
                return None
        return "list"
    # cat/head/tail/Get-Content of a file is left whole on purpose: the read guard marks such a dump as "in context"
    # and stops an identical one later, which is only true when the model saw all of it.
    return None


def classify(cmd):
    """The output families a command produces, or None when it should be left alone."""
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 2_000 or "<<" in cmd:
        return None
    if re.search(r"(^|[\s;&|(])(rtk|squeez|sudo|doas|ssh|su|runas|watch)(\s|$)", cmd):
        return None
    c = re.sub(r"\s*(2>&1|1>&2|>&2|2>\s*/dev/null|2>\s*\$null|2>\s*nul|\*>&1|2>\s*&1)", " ", cmd, flags=re.I)
    if re.search(r"[<>`]|\$\(|(?<![|])\|(?![|])|(?<![&>])&(?![&>])", c):
        return None                            # pipes, redirects, substitutions, background: already shaped by the caller
    fams = []
    for seg in re.split(r"&&|\|\||;|\n", c):
        toks = _toks(seg.strip())
        if not toks:
            continue
        head = _bare(toks[0])
        if head in NEUTRAL or re.match(r"^\$\w+(:\w+)?\s*=?$", toks[0]) or re.match(r"^[A-Za-z_]\w*=\S*$", toks[0]) and len(toks) == 1:
            continue
        if any(SKIP_FLAGS.match(_unq(w)) or FORMAT_FLAG.match(_unq(w)) for w in toks[1:]):
            return None
        if any(_unq(w).lower() in FORMAT_WORDS and _unq(toks[k]).startswith("-") for k, w in enumerate(toks[2:], 1)):
            return None                        # -o json, --format xml: the output is data
        f = family_of(toks)
        if f is None:
            return None
        if f not in fams:
            fams.append(f)
    return fams or None


READ_VERBS = {"cat", "nl", "bat", "less", "more", "type", "head", "tail", "sed", "grep", "egrep", "fgrep", "rg", "awk",
              "get-content", "gc", "select-string", "sls", "findstr"}


def is_file_read(cmd):
    """Every part of the command (cd/echo/export... aside) starts with a file-read verb, or is git show REV:PATH:
    its output is text the model asked to read, so the cap leaves it whole. Not a string: treated as a read."""
    if not isinstance(cmd, str):
        return True
    verbs = []
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        toks = strip_prefix(_toks(seg.split("|")[0].strip()))
        if not toks:
            continue
        head = _bare(toks[0])
        if head in NEUTRAL or re.match(r"^[A-Za-z_]\w*=", toks[0]):
            continue
        if head == "git" and _sub(toks)[0] == "show" and any(":" in _unq(w) and not _unq(w).startswith("-")
                                                            for w in toks[_sub(toks)[1] + 1:]):
            head = "cat"                       # git show REV:PATH prints a file
        verbs.append(head)
    return bool(verbs) and all(v in READ_VERBS for v in verbs)


TRIM_MAX = 200     # a final `| tail -N` / `| head -N` up to this many lines is the caller's own cut: the cap leaves it
# 2.0.9: ...only when the real output agrees. A line count does not bound size (`curl | head -1` is one line of any
# length), so the lines must also be normal width. How the bounds were chosen: the case 2.0.8 is for is a
# `| tail -200` of log, test or build lines, which run 80-200 chars (the 2.0.8 test's are ~150). LINE_CAP (400) is
# where the family shrink already calls a line over-long, so an average up to 400 leaves every such tail whole:
# 200 x 400 = 80k, past the ~30k Claude Code keeps inline, so no normal tail that reaches this hook is cut by it.
# One line over CAP_CHARS (4,000) is more than the whole cap shows: minified JS/JSON or a blob, not a line the
# caller sized, so it is cut whatever the average.
TRIM_LINE_AVG = LINE_CAP
TRIM_LINE_MAX = CAP_CHARS
TRIM_CMD_MAX = 4_000   # longer commands are not parsed for a trim (the cap applies as to any other command)
SILENT = {"cd", "export", "unset", "true", ":", "set", "set-location", "sl", "setlocal", "shopt", "mkdir", "umask",
          "ulimit"}    # parts that print nothing, so they may sit next to the trimmed pipeline (cd a && x | tail -5)
NEEDS_ARG = {"export", "set", "shopt", "umask", "ulimit"}    # ...but with no argument (or -p, -o, -a, -S) a table
TRIM_LONG = ("lines", "bytes", "quiet", "silent", "verbose", "follow", "retry", "pid", "sleep-interval",
             "max-unchanged-stats", "zero-terminated", "help", "version")   # head/tail long options (GNU)


def _split_top(cmd):
    """The top level of a shell command as [(part, op)], op being the operator after the part: '|' (also |&),
    ';' (also a newline), '&&', '||', '&' or '' at the end. Quotes, backslash escapes, comments, $(...), `...`,
    ( ) and { } groups stay inside their part. None when it is not plain enough to read: a heredoc, a case `;;`,
    an unclosed quote or bracket."""
    out, buf, stack, q, i, n = [], [], [], None, 0, len(cmd)

    def word_start():
        return not buf or buf[-1] in " \t\n;&|("

    while i < n:
        c = cmd[i]
        if q:                                          # inside quotes: only the closing quote (and \ in "..") counts
            buf.append(c)
            if c == "\\" and q != "'" and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 1
            elif c == q:
                q = None
            i += 1
            continue
        if c == "\\":
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        if c in "'\"`":
            q = c
        elif c == "#" and word_start():                # a comment, to the end of its line
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        elif cmd.startswith("<<", i):
            return None                                # a heredoc: its body would read as commands
        elif c == "{" and i and cmd[i - 1] == "$":
            stack.append("}$")                         # ${VAR}
        elif c == "{" and word_start() and (i + 1 >= n or cmd[i + 1] in " \t\n"):
            stack.append("}")                          # { a; b; }
        elif c == "(":
            stack.append(")")                          # ( a; b ), $(...), <(...)
        elif c == ")":
            if not stack or stack[-1] != ")":
                return None
            stack.pop()
        elif c == "}" and stack and (stack[-1] == "}$" or (stack[-1] == "}" and word_start())):
            stack.pop()
        elif not stack and c in ";&|\n":
            two = cmd[i:i + 2]
            if two == ";;":
                return None
            if c == "&" and buf and buf[-1] in "<>" or two == "&>":
                buf.append(c)                          # 2>&1, >&2, &>file: a redirect, not an operator
                i += 1
                continue
            op, step = {"&&": ("&&", 2), "||": ("||", 2), "|&": ("|", 2)}.get(two, (None, 1))
            op = op or {"|": "|", "&": "&"}.get(c, ";")
            out.append(("".join(buf).strip(), op))
            buf = []
            i += step
            continue
        buf.append(c)
        i += 1
    if q or stack:
        return None
    out.append(("".join(buf).strip(), ""))
    return out


def _silent(part):
    """A part that prints nothing to stdout: empty, VAR=value only, or cd/export/set... with what they need."""
    toks = [_unq(w) for w in _toks(part)]
    while toks and re.match(r"^[A-Za-z_]\w*=", toks[0]):
        toks = toks[1:]
    if not toks:
        return True
    head, args = _bare(toks[0]), toks[1:]
    if head not in SILENT:
        return False
    if head in ("cd", "set-location", "sl") and "-" in args:
        return False                                   # cd - prints the folder
    if head in NEEDS_ARG and (not args or args in (["-p"], ["-o"], ["+o"], ["-a"], ["-S"])):
        return False                                   # export / set -o / ulimit -a ...: a table
    return not (head == "mkdir" and any(re.match(r"^(-[a-z]*v|--verbose)", a) for a in args))


def _group_inner(part):
    """The inside of a part that is one ( ... ) or { ...; } group (stderr redirects after it allowed), or None."""
    p = part.strip()
    while True:
        m = re.search(r"\s+2>\s*(&1|/dev/null|\$null|nul)$", p, re.I)
        if not m:
            break
        p = p[:m.start()]
    if len(p) >= 2 and p[0] == "(" and p[-1] == ")":
        inner = p[1:-1]
    elif len(p) >= 3 and p[0] == "{" and p[1] in " \t\n" and p[-1] == "}":
        inner = p[1:-1]
    else:
        return None
    return inner if _split_top(inner) is not None else None


def _count(v):
    """A head/tail line count the caller sized: plain digits. -K (all but the last K), +K (from line K on),
    5K/1M multipliers or anything else: None."""
    return int(v) if isinstance(v, str) and re.fullmatch(r"\d{1,9}", v) else None


def _cut_count(stage):
    """Lines a `head`/`tail` stage reading stdin lets through, or None when it is not a small line cut: bytes
    (-c), follow (-f/-F), -n -K, +K, -r, -z, a file operand, a stdout redirect, --help, an unknown option."""
    toks = [_unq(w) for w in _toks(stage)]
    if not toks or _bare(toks[0]) not in ("head", "tail"):
        return None
    n, extra, args, i, done = 10, 0, toks[1:], 0, False
    while i < len(args):
        w = args[i]
        i += 1
        if re.match(r"^2>(&1|/dev/null|\$null|nul)?$", w, re.I):
            i += w == "2>"                             # stderr only: 2>&1, 2>/dev/null, 2> file
            continue
        if ">" in w or "<" in w:
            return None                                # stdout sent elsewhere, or input from a file
        if done or w == "-" or not w.startswith("-"):
            if w == "-":
                continue                               # `-` is stdin
            return None                                # a file operand (or tail's +K): not the pipe's cut
        if w == "--":
            done = True
        elif w.startswith("--"):
            name, eq, val = w[2:].partition("=")
            hits = [o for o in TRIM_LONG if o == name] or [o for o in TRIM_LONG if o.startswith(name)]
            if len(hits) != 1:
                return None
            if hits[0] == "lines":
                if not eq:
                    val = args[i] if i < len(args) else None
                    i += 1
                n = _count(val)
                if n is None:
                    return None
            elif hits[0] in ("quiet", "silent", "verbose"):
                extra = int(hits[0] == "verbose")      # -v prints a `==> standard input <==` line first
            else:
                return None                            # bytes, follow, retry, pid, zero-terminated, help...
        elif w[1].isdigit():                           # obsolete -5, -5q, tail -5l
            m = re.fullmatch(r"-(\d{1,9})([lqv]*)", w)
            if not m:
                return None                            # -5c (bytes), -5f (follow), -5k...
            n = int(m.group(1))
            extra = int("v" in m.group(2)) if re.search("[qv]", m.group(2)) else extra
        else:
            for k, ch in enumerate(w[1:], 1):
                if ch == "n":
                    val = w[k + 1:]
                    if not val:
                        val = args[i] if i < len(args) else None
                        i += 1
                    n = _count(val)
                    if n is None:
                        return None
                    break
                if ch in "qv":
                    extra = int(ch == "v")
                else:
                    return None                        # -c, -f, -F, -r, -z, -s...
    return n + extra if n <= TRIM_MAX else None


def trim_lines(cmd, _depth=0):
    """How many lines the command's own head/tail trim lets through, when that trim bounds everything it prints;
    else None. The command must be one pipeline ending in `| head -N` / `| tail -N` (N up to TRIM_MAX; none
    given: 10), next to nothing but silent parts (cd, export...). A ( ) or { } group in front of the trim is
    covered by it; a whole command in a group is read inside. This reads the text only: trim_fits checks it."""
    if not isinstance(cmd, str) or len(cmd) > TRIM_CMD_MAX or "|" not in cmd:
        return None
    parts = _split_top(cmd)
    if not parts:
        return None
    found, stages = None, []
    for part, op in parts:
        stages.append(part)
        if op == "|":
            continue
        if op == "&":
            return None                                # a background part prints when it likes
        if len(stages) == 1 and _silent(stages[0]):
            stages = []
            continue
        if found is not None or not all(stages):
            return None                                # a second part that prints, or an empty stage
        if len(stages) > 1:
            n = _cut_count(stages[-1])
        else:
            inner = _group_inner(stages[0])
            n = trim_lines(inner, _depth + 1) if inner is not None and _depth < 4 else None
        if n is None:
            return None
        found, stages = n, []
    return found


def is_trimmed(cmd):
    """The command reads as cut by its own final `| head -N` / `| tail -N` (see trim_lines)."""
    return trim_lines(cmd) is not None


def trim_fits(cmd, stdout):
    """The command reads as trimmed and its real output agrees: no more lines than the trim lets through, and
    normal-width lines (TRIM_LINE_AVG chars on average, none over TRIM_LINE_MAX). Earlier output that got past the
    trim (stderr merged in, a part the text reading missed) or a few huge lines fail it, and the cap applies."""
    n = trim_lines(cmd)
    if n is None or not isinstance(stdout, str):
        return False
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    lines = text.count("\n") + 1 if text else 0
    if lines > n or len(text) > TRIM_LINE_AVG * max(lines, 1):
        return False
    return max(map(len, text.split("\n"))) <= TRIM_LINE_MAX


# --- the cut ---------------------------------------------------------------------------------------------------------
def clean(line):
    """A line as the model should read it: carriage-return redraws resolved, color codes gone, tabs kept."""
    if "\r" in line:
        parts = [p for p in line.split("\r") if p.strip()]
        line = parts[-1] if parts else ""
    if "\x1b" in line:
        line = ANSI.sub("", line)
    return line.rstrip()


def is_binary(text):
    if "\x00" in text[:200_000]:
        return True
    sample = text[:20_000]
    bad = sum(1 for ch in sample if (ord(ch) < 32 and ch not in "\t\n\r\x1b\x0c\x08") or ch == "�")
    return bool(sample) and bad / len(sample) > 0.02


def cut_line(line):
    """An over-long line: its start, and when an error/warning word sits past the start, a window around it too."""
    if len(line) <= LINE_CAP:
        return line
    half = LINE_CAP // 2
    m = IMPORTANT.search(line, LINE_CAP - 60, 200_000)
    if not m or not m.group().strip():
        return f"{line[:LINE_CAP]} … (+{len(line) - LINE_CAP:,} chars)"
    a = max(half, m.start() - 40)
    b = min(len(line), a + half)
    out = line[:half] + (f" … (+{a - half:,} chars) … " if a > half else "") + line[a:b]
    return out + (f" … (+{len(line) - b:,} chars)" if b < len(line) else "")


def important(line):
    return bool(IMPORTANT.search(line)) and not NOT_IMPORTANT.match(line)


def git_stat(lines):
    """A diffstat computed from the diff itself: file, +added, -removed."""
    out, cur, hunk = [], None, False
    for ln in lines:
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", ln)
        if m:
            cur, hunk = [m.group(2), 0, 0], False
            out.append(cur)
        elif cur and ln.startswith("@@"):
            hunk = True
        elif cur and hunk and ln.startswith("+"):
            cur[1] += 1
        elif cur and hunk and ln.startswith("-"):
            cur[2] += 1
    return out


def list_summary(lines):
    """Entries per top folder (two levels), biggest first, for find/ls -R/tree output."""
    counts = {}
    for ln in lines:
        p = ln.strip().replace("\\", "/")
        if not p or p.endswith(":"):
            continue
        p = re.sub(r"^\./", "", p)
        parts = [x for x in p.split("/") if x]
        key = "/".join(parts[:2]) + "/" if len(parts) > 2 else ("/".join(parts[:1]) + "/" if len(parts) > 1 else "(top)")
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    return [f"  {n:>6,}  {k}" for k, n in top] + ([f"  … {len(counts) - 12:,} more folders"] if len(counts) > 12 else [])


SHAPES = {   # head lines, tail lines kept around the important ones
    "test": (5, 25), "install": (3, 15), "build": (5, 20), "git": (10, 10), "gitstatus": (40, 10),
    "gitlog": (60, 0), "gitdiff": (150, 0), "list": (40, 10), "log": (10, 40),
}


CONTENT = ("gitlog", "gitdiff", "list")     # the output IS the content: no error scan, no folding, just head + summary
FAMILY_BUDGET = {"gitlog": 4_000, "list": 3_000}   # an unbounded log or file list: the newest / first part is the point


def is_content(fams):
    """Every part prints content (git log/diff, a file list): no error scan. A test or build part keeps the scan."""
    return bool(fams) and all(f in CONTENT for f in fams)


def shrink_lines(raw, fams, budget=BUDGET):
    """(view lines, kept, total, omitted) for a list of raw lines.
    omitted: error/warning lines found but not in the view (the cap, or the cut to fit); the note counts them."""
    fams = fams or ["log"]
    content = is_content(fams)
    noise = [] if content else [NOISE_RE[k] for k in ("test", "install", "build", "git")
                                if k in fams or (k == "git" and "gitstatus" in fams)]
    if "test" in fams:
        noise.append(NOISE_RE["build"])        # cargo test / mvn test compile first
    lines = [clean(x) for x in raw]
    total = n = len(lines)
    keep = [False] * n
    drop = [False] * n
    for i, ln in enumerate(lines):
        if not ln.strip():
            drop[i] = i > 0 and not lines[i - 1].strip()      # runs of blank lines: one is enough
        elif not content and (any(r.search(ln) for r in noise) or PROGRESS.search(ln)):
            drop[i] = not important(ln) or ("test" in fams and bool(NOISE_RE["test"].search(ln)))
    fail_from = None                           # failure sections of a test run: everything from the first header on
    if "test" in fams:
        for i, ln in enumerate(lines):
            if FAIL_HEAD.search(ln):
                fail_from = i
                break
    if fail_from is not None:
        for i in range(fail_from, min(n, fail_from + FAIL_CAP)):
            keep[i], drop[i] = True, False
    imp = [] if content else [i for i, ln in enumerate(lines) if not drop[i] and important(ln)]
    found = set(imp)
    if len(imp) > IMPORTANT_CAP:
        imp = imp[:IMPORTANT_CAP // 2] + imp[-IMPORTANT_CAP // 2:]
    for i in imp:                              # important lines + their indented continuation (tracebacks), up to 12
        keep[i] = True
        if i > 0 and lines[i - 1].strip() and not drop[i - 1] and lines[i - 1].startswith((" ", "\t")):
            keep[i - 1] = True
        j = i + 1
        while j < n and j <= i + 12 and lines[j].strip() and lines[j][:1] in (" ", "\t", "|", "^", "~"):
            keep[j], drop[j] = True, False
            j += 1
    must = list(keep)
    shown = [i for i in range(n) if not drop[i]]
    hs = [SHAPES.get(f, (10, 20)) for f in fams]
    head, tail = max(h for h, _ in hs), max(t for _, t in hs)
    extra = summary(lines, fams, total, head + tail)
    while True:                                # head and tail shrink first: the error lines are the last to go
        keep = list(must)
        for i in shown[:head]:
            keep[i] = True
        for i in (shown[len(shown) - tail:] if tail else []):
            keep[i] = True
        view, kept, idx = assemble(lines, keep, n, fold=not content)
        view, idx = extra + view, [None] * len(extra) + idx
        if len("\n".join(view)) <= budget or (head <= 3 and tail <= 5):
            break
        head, tail = max(3, head // 2), max(5 if tail else 0, tail // 2)
    size = len("\n".join(view))
    if size > budget and len(view) > 40:       # still too big: errors themselves, one cut in the middle
        need = size - budget + 90              # + the marker line that replaces the cut
        lo_min, hi_max = len(extra) + 5, len(view) - 15
        lo = hi = max(lo_min, min(len(view) // 2, hi_max))
        removed = 0
        while removed < need and (lo > lo_min or hi < hi_max):     # grow the cut both ways until it is enough
            if hi < hi_max:
                removed += len(view[hi]) + 1
                hi += 1
            if removed < need and lo > lo_min:
                lo -= 1
                removed += len(view[lo]) + 1
        if hi - lo >= 2:
            gone = sum(1 for x in idx[lo:hi] if x is not None)
            view[lo:hi] = [f"… {gone:,} more kept lines cut to fit (they are in the full output) …"]
            idx[lo:hi] = [None]
            kept -= gone
    omitted = len(found - set(idx))
    return view, kept, total, omitted


def assemble(lines, keep, n, fold=True):
    """(view, kept, idx): kept lines in order, runs of near-identical ones folded, each hidden run named.
    idx[k] is the line number view[k] shows (None for a marker line)."""
    view, idx, hidden, kept, last_sig, rep = [], [], 0, 0, None, 0
    for i in range(n):
        if not keep[i]:
            hidden += 1
            continue
        sig = SIMILAR.sub("#", lines[i]).strip() if fold else None
        if fold and sig and sig == last_sig and not important(lines[i]):
            rep += 1
            continue
        if rep:
            view.append(f"… {rep:,} more like the line above …")
            idx.append(None)
            rep = 0
        if hidden:
            view.append(f"… {hidden:,} line{'s' if hidden != 1 else ''} hidden …")
            idx.append(None)
            hidden = 0
        view.append(cut_line(lines[i]))
        idx.append(i)
        kept += 1
        last_sig = sig
    if rep:
        view.append(f"… {rep:,} more like the line above …")
        idx.append(None)
    if hidden:
        view.append(f"… {hidden:,} line{'s' if hidden != 1 else ''} hidden …")
        idx.append(None)
    return view, kept, idx


def summary(lines, fams, total, shown):
    """A short header the cut makes possible: a diffstat, a commit count, entries per folder."""
    if "gitdiff" in fams:
        st = git_stat(lines)
        if st:
            return [f"diff: {len(st)} file{'s' if len(st) != 1 else ''}, +{sum(s[1] for s in st):,} -{sum(s[2] for s in st):,}"] + \
                   [f"  {s[0]}  +{s[1]:,} -{s[2]:,}" for s in st[:40]] + ([f"  … {len(st) - 40:,} more files"] if len(st) > 40 else [])
    if "gitlog" in fams:
        commits = sum(1 for ln in lines if re.match(r"^commit [0-9a-f]{7,}", ln))
        return [f"log: {commits:,} commits in the full output" if commits else f"log: {total:,} lines in the full output"]
    if "list" in fams and total > shown:
        paths = [ln for ln in lines if "/" in ln or "\\" in ln]
        if len(paths) * 2 >= len(lines):
            return [f"entries: {total:,}, by folder:"] + list_summary(lines)
        return [f"entries: {total:,} lines"]
    return []


def tokens(chars):
    return int(chars) // 4


def native_view(stdout, response):
    """Characters of the view Claude Code itself would give the model for this result."""
    if isinstance(response, dict) and response.get("persistedOutputPath"):
        return PREVIEW_CHARS + 160 + len(C.dstr(response.get("persistedOutputPath")))
    return len(stdout)


def out_dir():
    if os.name == "nt":                          # inside Big Dad's own state folder (bd_dir()/state, 2.0.9)
        return os.path.join(C.state_dir(), "bigdad-out")
    return os.path.join(C.state_dir(), f"bigdad-out-{os.getuid()}")


def _private(d):
    """Make the output folder; False when it is not ours alone (then nothing is saved or cut)."""
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):           # a symlink (lstat) is not ours to write through
        return False
    if os.name == "nt":                        # a junction or other reparse point: same; the ACL is inherited from the
        # state folder (bd_dir()/state, 2.0.9) and not checked here; `health` reads the state folder's ACL
        return not getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if st.st_uid != os.getuid():
        return False
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)
    return True


OWN_NAME = re.compile(r"^[A-Za-z0-9]{1,8}-\d{8}-\d{6}-[0-9a-f]{8}-\d+(-\d{1,2})?\.txt$")   # {sid8}-{date}-{time}-{crc32}-{pid}[-k].txt
SAVE_TRIES = 100   # 2.0.9: the same output twice in one second (same crc, second and pid) gets -1, -2 ... -99


def save_full(text, sid):
    """Write the full output to a new file in the state folder; its path, or "" when it can't be saved (then nothing
    is cut). Never overwrites: the name is created with O_EXCL, and a name already taken (the same output saved
    earlier in the same second by this process) gets the next -k suffix."""
    d = out_dir()
    try:
        if not _private(d):
            return ""
        stamp = os.path.join(d, ".cleaned")
        try:
            due = time.time() - os.path.getmtime(stamp) > 6 * 3600
        except OSError:
            due = True
        if due:
            open(stamp, "w").close()
            for f in os.listdir(d):
                p = os.path.join(d, f)
                try:
                    if OWN_NAME.match(f) and time.time() - os.path.getmtime(p) > 3 * 86400:   # only files it wrote
                        os.remove(p)
                except OSError:
                    pass
        data = text.encode("utf-8", "replace")
        stem = f"{re.sub(r'[^A-Za-z0-9]', '', sid)[:8] or 'x'}-{time.strftime('%Y%m%d-%H%M%S')}-" \
               f"{zlib.crc32(data) & 0xffffffff:08x}-{os.getpid()}"
        for k in range(SAVE_TRIES):
            p = os.path.join(d, f"{stem}-{k}.txt" if k else f"{stem}.txt")
            try:
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            except FileExistsError:
                continue                               # taken: never written over, try the next name
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return p
        return ""
    except (OSError, ValueError):
        return ""


def disabled():
    if C.off("BIGDAD_SHRINK"):
        return True
    try:
        if __import__("bd_keep").verbose():           # `keep verbose`: the user chose full output
            return True
    except Exception:
        pass
    try:
        with open(os.path.join(C.bd_dir(), "shrink"), encoding="utf-8", errors="replace") as f:
            return f.read().strip().lower() in ("off", "0", "false")
    except OSError:
        return False


def note(kept, total, before, after, path, fams=(), omitted=0, what=None):
    where = f"Full output: {path} (Grep it, or Read it with offset/limit)."
    what = what or ("the start and a summary kept" if is_content(fams) else "errors, warnings and the end kept")
    if omitted:
        what += f", but {omitted:,} more error/warning line{'s' if omitted != 1 else ''} did not fit (only in the full output)"
    return (f"[🥛 {MARK}: showed {kept:,} of {total:,} lines (~{tokens(before):,} → ~{tokens(after):,} tokens): {what}. {where}]")


NOTE_RE = re.compile(r"Big Dad shrink: showed ([\d,]+) of ([\d,]+) lines \(~([\d,]+) → ~([\d,]+) tokens")


def parse_note(text):
    """(before, after) tokens from a shrink note in a tool result, or None."""
    m = NOTE_RE.search(text) if isinstance(text, str) else None
    return (int(m.group(3).replace(",", "")), int(m.group(4).replace(",", ""))) if m else None


DRY_PATH = os.path.join("bigdad-out", "not-saved-while-learning.txt")   # same length class as a real path


def shrink_response(event, dry=False):
    """The updated tool output for a PostToolUse event, plus the receipt row; None to leave it whole.
    dry (learning first, 2.0.6): the same verdict with no full output saved."""
    if event.get("tool_name") not in SHELLS or event.get("hook_event_name") not in (None, "PostToolUse"):
        return None
    args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    resp = event.get("tool_response")
    if not isinstance(resp, dict) or args.get("run_in_background") or resp.get("interrupted") or resp.get("isImage"):
        return None
    stdout = resp.get("stdout")
    if not isinstance(stdout, str) or not isinstance(resp.get("stderr", ""), str):
        return None
    if MARK in stdout or resp.get("backgroundTaskId"):
        return None
    if resp.get("persistedOutputPath"):
        return None                                   # Claude Code saved it and shows its own 2KB preview (LEDGER #9)
    fams = classify(args.get("command"))
    if not fams:
        return cap_response(event, args, resp, stdout, dry)
    if len(stdout) < MIN_CHARS or stdout.count("\n") < MIN_LINES:
        return None
    raw, full_chars, budget = stdout.split("\n"), len(stdout), BUDGET
    budget = min([budget] + [FAMILY_BUDGET[f] for f in fams if f in FAMILY_BUDGET])
    text_probe = "\n".join(raw[:2000])
    if is_binary(text_probe):
        return None
    if raw and raw[-1] == "":
        raw = raw[:-1]
    view, kept, total, omitted = shrink_lines(raw, fams, budget)
    before = len(stdout)
    body = "\n".join(view)
    path = DRY_PATH if dry else save_full(stdout, C.dstr(event.get("session_id")))
    if not path:
        return None
    text = body + "\n" + note(kept, total, before, len(body) + 200, path, fams, omitted)
    after = len(text)
    text = body + "\n" + note(kept, total, before, after, path, fams, omitted)
    after = len(text)
    if tokens(before) - tokens(after) < MIN_SAVE_TOKENS or after > (1 - MIN_SAVE_SHARE) * before:
        try:
            if not dry:
                os.remove(path)                       # our own copy only
        except OSError:
            pass
        return None
    new = dict(resp)
    new["stdout"] = text
    row = {"ts": C.now(), "session": C.dstr(event.get("session_id"))[:8], "tool": event.get("tool_name"),
           "family": "+".join(fams), "lines": total, "kept": kept, "before": tokens(before), "after": tokens(after),
           "full_tokens": tokens(full_chars), "omitted": omitted}
    return new, row


def cap_view(text, note_len):
    """(start, end) of an over-long output, cut at line ends when one is near. The end is sized so the note and
    the end together stay inside the last 1,500 chars the receipts read (with room for Claude Code's cwd line)."""
    tail_n = max(200, min(CAP_TAIL, 1_300 - note_len))
    head_n = CAP_CHARS - tail_n
    cut = text.rfind("\n", 0, head_n)
    head = text[:cut] if cut >= head_n // 2 else text[:head_n]
    start = text.find("\n", len(text) - tail_n)
    tail = text[start + 1:] if 0 <= start < len(text) - tail_n // 2 else text[len(text) - tail_n:]
    return head, tail


def cap_response(event, args, resp, stdout, dry=False):
    """2.0.5 cap: a command no family knows, not a file read, over CAP_CHARS: the start and the end kept, the
    note where the middle was. Same note, saved copy and receipt row as the family shrink; None to leave it.
    2.0.9: a command cut by its own `| head -N` / `| tail -N` is left whole only when its real output agrees
    (trim_fits); when it doesn't, the cap applies, to a file read too (`cat app.min.js | head -3` asked for 3
    lines, not the file; the read guard sees the note and does not mark the file as in context)."""
    cmd = args.get("command")
    if len(stdout) < CAP_CHARS + 4 * MIN_SAVE_TOKENS + 200:
        return None                                   # (+ the net win and the note: no copy saved just to delete it)
    if trim_lines(cmd) is not None:
        if trim_fits(cmd, stdout):
            return None
    elif is_file_read(cmd):
        return None
    if is_binary(stdout[:200_000]):
        return None
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    total = text.count("\n") + 1
    before = len(stdout)
    what = f"output over {CAP_CHARS:,} chars, the start and the end kept"
    path = DRY_PATH if dry else save_full(stdout, C.dstr(event.get("session_id")))
    if not path:
        return None
    head, tail = cap_view(text, len(note(total, total, before, before, path, what=what)))
    kept = min(total, head.count("\n") + tail.count("\n") + 2)
    after = len(head) + len(tail) + 400
    for _ in range(2):                                # the note carries its own size: settle it
        out = head + "\n" + note(kept, total, before, after, path, what=what) + "\n" + tail
        after = len(out)
    if tokens(before) - tokens(after) < MIN_SAVE_TOKENS:
        try:
            if not dry:
                os.remove(path)                       # our own copy only
        except OSError:
            pass
        return None
    new = dict(resp)
    new["stdout"] = out
    row = {"ts": C.now(), "session": C.dstr(event.get("session_id"))[:8], "tool": event.get("tool_name"),
           "family": "cap", "lines": total, "kept": kept, "before": tokens(before), "after": tokens(after),
           "full_tokens": tokens(before), "omitted": 0}
    return new, row


def main(event=None, standalone=True):
    try:
        if disabled():
            return None
        event = C.read_event() if event is None else event
        if not isinstance(event, dict):
            return None
        if C.learning():                              # learning first (2.0.6): counted; nothing cut, saved or logged
            if shrink_response(event, dry=True) is not None:
                C.learn_seen("big_output")
            return None
        r = shrink_response(event)
        if r is None:
            return None
        new, row = r
        C.append_jsonl(os.path.join(C.bd_dir(), "shrinks.jsonl"), row)
        return C.emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}}, standalone)
    except Exception:
        return None                                   # fail open: Claude sees the original output


# --- `bigdad.py shrink on|off|status` ---------------------------------------------------------------------------------
def cmd(rest):
    v = (rest[0] if rest else "status").lower()
    f = os.path.join(C.bd_dir(), "shrink")
    if v in ("on", "off"):
        os.makedirs(os.path.dirname(f), exist_ok=True)
        if v == "on":
            try:
                C.remove_file(f)
            except C.Held:
                raise                   # still held after the wait: say so, never "on" with the switch left off
            except OSError:
                pass
        else:
            with C.open_wait(f, "w", encoding="utf-8") as fh:
                fh.write("off\n")
        print(f"Big Dad output shrink: {v}. Applies from the next command." +
              (" Big outputs are shown whole again." if v == "off" else ""))
        return
    rows = []
    try:
        with open(os.path.join(C.bd_dir(), "shrinks.jsonl"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("before"), int) and isinstance(d.get("after"), int):
                    rows.append(d)
    except OSError:
        pass
    state = "off" + (" (BIGDAD_SHRINK)" if C.off("BIGDAD_SHRINK") else "") if disabled() else "on"
    print(f"Big Dad output shrink: {state}. Off: bigdad.py shrink off (or BIGDAD_SHRINK=off).")
    if not rows:
        print("No shrunk outputs logged yet.")
        return
    fam = {}
    for d in rows:
        k = str(d.get("family") or "?").split("+")[0]
        a = fam.setdefault(k, [0, 0, 0])
        a[0] += 1
        a[1] += d["before"]
        a[2] += d["after"]
    b, a = sum(d["before"] for d in rows), sum(d["after"] for d in rows)
    print(f"{len(rows):,} outputs shrunk: ~{b:,} → ~{a:,} tokens as first sent ({100 * (b - a) / max(b, 1):.0f}% less; "
          "estimated, ~4 chars per token; each later call re-sends the smaller copy, not multiplied out here)")
    for k, (n, bb, aa) in sorted(fam.items(), key=lambda kv: -(kv[1][1] - kv[1][2])):
        print(f"  {k:<10} {n:>5,}  ~{bb:>9,} → ~{aa:>8,}  {100 * (bb - aa) / max(bb, 1):>3.0f}%")


if __name__ == "__main__":
    main()
