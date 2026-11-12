"""Big Dad versus stock Claude Code: the A/B harness and its receipt (shared spec: guild/versus-spec.md, v1).

Two arms, same command, same fixture, same prompt, interleaved N,B,N,B,...:
  N  stock Claude Code, nothing of your setup: a fresh config dir, home and working folder per run under a root
     outside your home folder (default C:\\bdversus, /tmp/bdversus elsewhere), `--setting-sources ""` (no settings
     files, so none of your plugins), `--strict-mcp-config` (no MCP servers), an allow-listed environment (no
     GH_TOKEN, cloud keys, CLAUDE*/ANTHROPIC*/BIGDAD*/MCP* of yours), only the login's access token copied in for
     each call and deleted after (bd_isolation.py, the bench's proven isolation)
  B  the same, plus Big Dad loaded ONCE from a frozen snapshot of one commit (git archive -> run root -> --plugin-dir)
Every run's init event is checked against stock (plugins, skills, agents, MCP servers, no hooks in N) and its
transcript scanned for names from your real setup; problems are recorded per run and shown on the receipt.
Every number is measured from the stream-json Claude Code prints (assistant `usage`, `result`, hook events), from a
clock around the process, or from the hook scripts timed on canned events. Nothing is estimated unless labelled so
(Big Dad's notes: chars/4 ~ tokens). Headless /compact is tried on each session; when Claude Code logs no
compact_boundary the post-compact floor is "not measured".

  bigdad.py versus run [--runs 3] [--out DIR] [--transcript LOG] [--commit 427c054] [--cap 4m] [--root C:\\bdversus]
                       [--learning]
  bigdad.py versus receipt [DIR]           VERSUS-RECEIPT.md + receipt.txt from DIR/versus-runs.json, no Claude calls

The root comes from --root, else BIGDAD_VERSUS_ROOT, else the default; a root inside your home folder or under a
CLAUDE.md / AGENTS.md / .claude is refused. Each run's folder (config, home, cwd, Big Dad state) is deleted after;
sessions never land in your real ~/.claude/projects. Hook timing runs under a temp HOME. The real ~/.claude/bigdad
is watched before and after and the result is on the receipt. Output carries no home path, session id, email or
user name (`~` stands for the home folder).

Big Dad's state in B (2.0.7): each B run starts with a fresh state folder, which a 2.0.6+ Big Dad treats as a new
install and only watches (its learning period). So B runs with BIGDAD_LEARN=off and acts from the first call;
`--learning` leaves it learning instead, to measure what a new install does. N never gets the switch. The receipt
names the state, and a B run whose session start line says the other state is a problem run.
"""
import glob, hashlib, io, json, os, platform, random, re, shutil, statistics, subprocess, sys, tarfile, tempfile, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, HERE)
import bd_common as C  # noqa: E402
import bd_isolation as I  # noqa: E402

SPEC = "versus-spec v1"
COMMIT = "427c054"                       # the 2.0.6 release
DEFAULT_ROOT = r"C:\bdversus" if os.name == "nt" else "/tmp/bdversus"
ISOLATION = "stock"                      # params["isolation"]: runs before 1.7.0 carry "settings" instead (your setup)
PROMPT = ('''Read notes.md. Then read log.txt. Then read notes.md again. Then run the Bash command '''
          '''`python -c "print('row,'*4000)"`. Then read log.txt again. Then reply with just the number of lines '''
          '''in notes.md and in log.txt.''')
GUARD_AT = 20_000
STATES = ("acting", "learning")          # params["bigdad_state"]: Big Dad's state in B (absent before 2.0.7)
LEARNING_LINE = "Big Dad is learning first"   # session_start.py's line while learning
MAX_TURNS = 14
ALLOWED = "Read Bash"
MODEL = "haiku"
SEED = 7
CAP = 4_000_000                          # token cap: input + output over all sessions, compact legs included
NOTES_BYTES, LOG_BYTES = 40_000, 60_000
W = 66                                   # the receipt's column width, same as bd_money.py
WORDS = ("the quick brown fox jumps over a lazy dog while the river keeps its own slow time and nobody counts "
         "the stones under the bridge because every morning brings another list of small repairs that wait for "
         "hands and light and patience until the season turns again toward frost").split()
SERVICES = ("api", "worker", "db", "cache", "auth", "queue", "web", "cron")
LEVELS = ("INFO", "INFO", "INFO", "WARN", "DEBUG", "ERROR")
HOOK_FILES = ("hooks/*.py", "hooks/*.sh", "hooks/hooks.json")
SCRIPT_FILES = ("skills/bigdad/scripts/*.py", "skills/bigdad/SKILL.md")
from bd_native import NATIVE, NATIVE_LACKS  # noqa: E402  one table with fresh: (native lever, Big Dad part), what none does


# ----------------------------------------------------------------------------------------------- fixture + snapshot
def fixture(cwd, seed=SEED):
    """notes.md (~40 KB prose lines) and log.txt (~60 KB log lines), the same bytes for the same seed."""
    rnd = random.Random(seed)
    notes, size = [], 0
    while size < NOTES_BYTES:
        words = [rnd.choice(WORDS) for _ in range(rnd.randrange(8, 15))]
        line = " ".join(words).capitalize() + "."
        notes.append(line)
        size += len(line) + 1
    log, size = [], 0
    while size < LOG_BYTES:
        line = (f"2026-01-{rnd.randrange(1, 29):02d} {rnd.randrange(24):02d}:{rnd.randrange(60):02d}:{rnd.randrange(60):02d} "
                f"[{rnd.choice(LEVELS)}] {rnd.choice(SERVICES)}: {' '.join(rnd.choice(WORDS) for _ in range(rnd.randrange(4, 9)))} "
                f"id={rnd.randrange(10 ** 6):06d} ms={rnd.randrange(5000)}")
        log.append(line)
        size += len(line) + 1
    out = {}
    for name, lines in (("notes.md", notes), ("log.txt", log)):
        data = ("\n".join(lines) + "\n").encode("ascii")
        with open(os.path.join(cwd, name), "wb") as f:
            f.write(data)
        out[name] = {"lines": len(lines), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]}
    return out


def snapshot(commit, dest, repo=ROOT):
    """`git archive <commit>` of the repo into dest: a frozen copy for --plugin-dir. Returns the full commit hash."""
    full = subprocess.run(["git", "-C", repo, "rev-parse", commit], capture_output=True, text=True, check=True).stdout.strip()
    data = subprocess.run(["git", "-C", repo, "archive", "--format=tar", commit], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)
    if not os.path.isfile(os.path.join(dest, "hooks", "hooks.json")):
        raise RuntimeError("snapshot has no hooks/hooks.json")
    return full


def arm_extra(arm, bd_state="acting"):
    """The variables a run adds: BIGDAD_GUARD_AT=20000 in both arms; B acting also BIGDAD_LEARN=off (its state
    folder is fresh, so without it Big Dad would only watch). N gets nothing else: it stays stock."""
    extra = {"BIGDAD_GUARD_AT": str(GUARD_AT)}
    if arm == "B" and bd_state == "acting":
        extra["BIGDAD_LEARN"] = "off"
    return extra


def arm_env(cfg, home, state, arm="N", bd_state="acting"):
    """Both arms: the run's own config dir, home and Big Dad state dir (bd_isolation.run_env passes only the
    ENV_KEEP allow-list from the host and points the Windows profile names into the temp home), then arm_extra."""
    return I.run_env(cfg, home, state, arm_extra(arm, bd_state))


def claude_cmd(arm, snap, extra):
    """Stock Claude Code: no settings files of any scope, no MCP servers; B adds the snapshot with --plugin-dir."""
    cmd = [shutil.which("claude") or "claude", "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
           "--include-hook-events", *I.FLAGS, "--allowedTools", ALLOWED, "--max-turns", str(MAX_TURNS)]
    if arm == "B":
        cmd += ["--plugin-dir", snap]
    return cmd + list(extra)


def run_dirs(root, i, arm):
    """A fresh run folder under the versus root: home/.claude (config), work (cwd), state (Big Dad's state files)."""
    run_root = tempfile.mkdtemp(prefix=f"run{i}-{arm}-", dir=root)
    home, work, state = (os.path.join(run_root, n) for n in ("home", "work", "state"))
    cfg = os.path.join(home, ".claude")
    for d in (cfg, work, state):
        os.makedirs(d)
    return run_root, cfg, home, work, state


def rm_tree(d):
    """rmtree that also removes read-only files (git objects on Windows)."""
    def fix(fn, path, *_):
        try:
            os.chmod(path, 0o700)
            fn(path)
        except OSError:
            pass
    if os.path.exists(d):
        shutil.rmtree(d, **({"onexc": fix} if sys.version_info >= (3, 12) else {"onerror": fix}))


def no_auth_left(root):
    """Login files still under the versus root (must be none): deleted on sight, paths returned."""
    return I.sweep_logins(root)


# ----------------------------------------------------------------------------------------------- stream parsing
def hook_text(output):
    """The text a Big Dad hook output carries (what Claude or the user gets), out of its JSON wrapper."""
    s = output or ""
    try:
        o = json.loads(s)
    except ValueError:
        return s
    if not isinstance(o, dict):
        return s
    h = o.get("hookSpecificOutput") if isinstance(o.get("hookSpecificOutput"), dict) else {}
    return " ".join(str(x) for x in (o.get("systemMessage"), h.get("additionalContext"), h.get("permissionDecisionReason"),
                                     o.get("reason")) if x)


def parse_stream(text):
    """One `claude -p` stream-json run -> calls (one per message id), tools, hook counts, Big Dad outputs, result."""
    r = {"calls": [], "tools": [], "hooks_started": {}, "bd_outputs": [], "denied_reads": 0, "compact": None,
         "result": None, "session_id": "", "model": "", "init": None, "bd_start": []}
    seen = set()
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "assistant":
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            u = m.get("usage") if isinstance(m.get("usage"), dict) else {}
            mid = m.get("id")
            if u and mid not in seen:
                seen.add(mid)
                cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
                cw = int(u.get("cache_creation_input_tokens") or 0)
                w1h = min(cw, int(cc.get("ephemeral_1h_input_tokens") or 0))
                r["calls"].append({"new": int(u.get("input_tokens") or 0), "w5": cw - w1h, "w1h": w1h,
                                   "read": int(u.get("cache_read_input_tokens") or 0), "out": int(u.get("output_tokens") or 0)})
                r["model"] = r["model"] or str(m.get("model") or "")
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and not (c.get("id") and c.get("id") in seen):
                    seen.add(c.get("id"))                    # the same call logged twice: count it once
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    what = os.path.basename(str(inp.get("file_path") or "")) if c.get("name") == "Read" else ""
                    r["tools"].append(f"{c.get('name')}:{what}" if what else str(c.get("name")))
        elif t == "user":
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            body = m.get("content")
            flat = body if isinstance(body, str) else " ".join(
                str(c.get("text") or "") for c in body if isinstance(c, dict)) if isinstance(body, list) else ""
            # headless /compact: PreCompact never shows as a hook event; its output comes back as local-command-stdout
            if "<local-command-stdout>" in flat and "PreCompact" in flat and "Big Dad" in flat:
                pc = flat.split("completed successfully:", 1)[-1].split("</local-command-stdout>", 1)[0]
                r["bd_outputs"].append({"event": "PreCompact", "chars": len(pc.strip())})
            for c in body if isinstance(body, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error") is True:
                    inner = c.get("content")
                    s = inner if isinstance(inner, str) else json.dumps(inner or "")
                    if C.is_read_deny(s):
                        r["denied_reads"] += 1
        elif t == "system":
            st = d.get("subtype")
            name = str(d.get("hook_name") or d.get("hook_event") or "?")
            if st == "hook_started":
                r["hooks_started"][name] = r["hooks_started"].get(name, 0) + 1
            elif st == "hook_response":
                out = str(d.get("output") or d.get("stdout") or "")
                if "Big Dad" in out:
                    r["bd_outputs"].append({"event": name, "chars": len(hook_text(out))})
                    if name.startswith("SessionStart:startup"):     # the start line says which state Big Dad is in
                        r["bd_start"].append("learning" if LEARNING_LINE in hook_text(out) else "acting")
            elif st == "compact_boundary":
                meta = d.get("compact_metadata") or d.get("compactMetadata") or {}
                r["compact"] = {k: meta.get(k) for k in ("trigger", "pre_tokens", "post_tokens", "duration_ms")} \
                    if isinstance(meta, dict) else {}
            elif st == "init":
                r["session_id"] = str(d.get("session_id") or "")
                r["init"] = r["init"] or d
        elif t == "result":
            u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
            r["result"] = {"duration_ms": int(d.get("duration_ms") or 0),
                           "duration_api_ms": int(d.get("duration_api_ms") or 0), "num_turns": int(d.get("num_turns") or 0),
                           "is_error": bool(d.get("is_error")), "subtype": str(d.get("subtype") or ""),
                           "answer": str(d.get("result") or "")}         # whole reply: the check reads all of it, the record keeps 400 chars
            r["session_id"] = r["session_id"] or str(d.get("session_id") or "")
    return r


def total_input(call):
    return call["new"] + call["w5"] + call["w1h"] + call["read"]


# ----------------------------------------------------------------------------------------------- the A/B runs
def run_tokens(r):
    """Every token one `claude -p` process moved: input (new + written + read) and output, all calls."""
    return sum(total_input(c) + c["out"] for c in r["calls"])


class Budget:
    """Token cap over all runs (tokens only: Big Dad prints no prices)."""

    def __init__(self, cap):
        self.cap, self.sessions, self.stopped = cap, {}, False

    def note(self, sid, tokens):
        self.sessions[sid] = self.sessions.get(sid, 0) + tokens

    @property
    def used(self):
        return sum(self.sessions.values())


def call_claude(cmd, cwd, env, log):
    t0 = time.perf_counter()
    p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=900)
    wall = time.perf_counter() - t0
    text = p.stdout.decode("utf-8", "replace")
    if log:
        with open(log, "ab") as f:
            f.write(p.stdout + b"\n" + p.stderr + b"\n")
    r = parse_stream(text)
    r["wall_ms"] = int(wall * 1000)
    r["rc"] = p.returncode
    r["stderr"] = p.stderr.decode("utf-8", "replace")[-300:]
    return r


def call_isolated(cmd, cwd, env, log, cfg, auth):
    """One claude call with the login in the run's config dir only while it runs (deleted after, even on a crash)."""
    with I.Auth(cfg, auth):
        return call_claude(cmd, cwd, env, log)


def transcripts(cfg):
    """Every session log Claude Code wrote under the run's own config dir."""
    out = []
    for r, _, files in os.walk(os.path.join(cfg, "projects")):
        out += [os.path.join(r, f) for f in files if f.endswith(".jsonl")]
    return sorted(out)


def plugin_text(folder, _cache={}):
    """All text of the Big Dad snapshot (cached): words Big Dad itself can say are not a leak in its arm."""
    if folder not in _cache:
        parts = []
        for r, _, files in os.walk(folder):
            for f in files:
                if f.endswith((".py", ".md", ".json", ".sh", ".txt")):
                    with open(os.path.join(r, f), encoding="utf-8", errors="replace") as fh:
                        parts.append(fh.read())
        _cache[folder] = "\n".join(parts)
    return _cache[folder]


def isolation_problems(ctx, arm, legs, cfg, snap):
    """Problems with one run's isolation (empty = stock Claude Code, plus exactly Big Dad in B), from every leg's init
    event, the hooks that ran, and the run's own transcripts. The first native run with an init sets the stock skill
    baseline (as the bench's preflight does) and drops the marker names stock Claude Code itself says."""
    has = arm == "B"
    inits = [leg.get("init") for leg in legs]
    bad, first_base = [], False
    if not has and ctx.get("baseline") is None and isinstance(inits[0], dict):
        ctx["baseline"], first_base = sorted(inits[0].get("skills") or []), True
    for init in inits:
        base = ctx.get("baseline")
        if base is None:
            bad.append("skills not checked: no stock baseline yet")
            base = [s for s in (init or {}).get("skills") or [] if s != I.BD_SKILL]
        bad += [x for x in I.check_init(init, has, base) if x not in bad]
        if not has and isinstance(init, dict):
            extra = [s for s in init.get("skills") or [] if ":" in s]
            if extra:
                bad.append(f"plugin skills in the native arm: {extra}")
    hooks = sum(sum(leg.get("hooks_started", {}).values()) for leg in legs)
    if not has and hooks:
        bad.append(f"{hooks} hook runs in the native arm")
    if ctx.get("markers"):
        texts = [json.dumps(x) for x in inits if x]
        for p in transcripts(cfg):
            with open(p, encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
        names, heads = ctx["markers"]
        if first_base and not bad:     # structure proves this run stock: any owner name in it is a stock/fixture word
            init = inits[0]
            stock = set(init.get("skills") or []) | set(init.get("agents") or []) | set(init.get("tools") or []) | \
                set(init.get("slash_commands") or [])
            common = set(I.scan_leaks(texts, names, heads)) | (set(names) & stock)
            ctx["markers"] = ([n for n in names if n not in common], [h for h in heads if "CLAUDE.md: " + h not in common])
            ctx["markers_dropped"] = sorted(common)
        leaks = I.scan_leaks(texts, *ctx["markers"])
        if has and leaks:
            own = set(I.scan_leaks([plugin_text(snap)], *ctx["markers"]))
            leaks = [x for x in leaks if x not in own]
        if leaks:
            bad.append(f"names from your real setup in the run: {leaks}")
    return bad


def state_seen(legs):
    """Big Dad's state as its session start line showed it: "learning" (the learning line), "acting" (another Big
    Dad start line), None (no Big Dad start line)."""
    seen = [s for leg in legs for s in leg.get("bd_start") or []]
    return "learning" if "learning" in seen else "acting" if seen else None


def state_problem(arm, seen, want):
    """A B run whose start line shows the other state than the run was set to (None: nothing to compare)."""
    if arm != "B" or not seen or not want or seen == want:
        return None
    if seen == "learning":
        return ("Big Dad said it was learning first, but B was set to act (BIGDAD_LEARN=off): "
                "its numbers are the learning period's")
    return ("Big Dad started acting, but B was set to learning (new-install state): "
            "its numbers are not the learning period's")


def answer_ok(answer, fx):
    """True when the reply names both fixture line counts as whole numbers (a wrong count is a wrong answer)."""
    nums = set(re.findall(r"\d+", answer or ""))
    return all(str(fx[name]["lines"]) in nums for name in ("notes.md", "log.txt"))


def one_run(i, arm, snap, budget, say, raw_dir=None, ctx=None):
    """One session of one arm in its own run folder under ctx["root"] (login ctx["auth"]); the folder is deleted
    after, the login file first, whatever happens. The run's isolation problems are recorded on it."""
    run_root, cfg, home, cwd, state = run_dirs(ctx["root"], i, arm)
    legs = []
    want = ctx.get("bd_state", "acting")
    try:
        run = _legs(i, arm, snap, budget, raw_dir, ctx, cfg, cwd, arm_env(cfg, home, state, arm, want), legs)
        run["isolation"] = isolation_problems(ctx, arm, legs, cfg, snap)
        if arm == "B":
            run["bd_state_seen"] = state_seen(legs)
            bad = state_problem(arm, run["bd_state_seen"], want)
            if bad:
                run["isolation"].append(bad)
        run["clean"] = not run["isolation"]
        run["plugins"] = sorted({p.get("source") for leg in legs if isinstance(leg.get("init"), dict)
                                 for p in leg["init"].get("plugins") or [] if isinstance(p, dict)})
        return run
    finally:
        try:
            os.remove(os.path.join(cfg, ".credentials.json"))
        except OSError:
            pass
        rm_tree(run_root)


def _legs(i, arm, snap, budget, raw_dir, ctx, cfg, cwd, env, legs):
    """The main run, then the compact leg (`/compact`, `reply ok`) on the same session; every leg lands in legs."""
    log = os.path.join(raw_dir, f"run{i}-{arm}.stream.jsonl") if raw_dir else None

    def call(extra):
        legs.append(call_isolated(claude_cmd(arm, snap, extra), cwd, env, log, cfg, ctx["auth"]))
        return legs[-1]

    fx = fixture(cwd)
    main = call([PROMPT])
    sid = main["session_id"]
    ctx.setdefault("sids", []).append(sid)
    res = main["result"] or {}
    budget.note(sid or f"run{i}", run_tokens(main))
    run = {"i": i, "arm": arm, "fixture": fx, "rc": main["rc"], "wall_ms": main["wall_ms"],
           "duration_ms": res.get("duration_ms", 0), "duration_api_ms": res.get("duration_api_ms", 0),
           "turns": res.get("num_turns", 0), "is_error": res.get("is_error", True),
           "subtype": res.get("subtype", ""), "answer": str(res.get("answer", ""))[:400], "answer_ok": answer_ok(res.get("answer", ""), fx),
           "subagent_stop": main["hooks_started"].get("SubagentStop", 0), "model": main["model"],
           "calls": len(main["calls"]), "per_call_input": [total_input(c) for c in main["calls"]],
           "tokens": {k: sum(c[k] for c in main["calls"]) for k in ("new", "w5", "w1h", "read", "out")},
           "tools": main["tools"], "denied_reads": main["denied_reads"], "hooks_started": main["hooks_started"],
           "bd_outputs": len(main["bd_outputs"]),
           "bd_briefs": sum(1 for o in main["bd_outputs"] if o["event"].startswith("SessionStart:startup")),
           "bd_note_chars": sum(o["chars"] for o in main["bd_outputs"]),
           "bd_events": sorted(set(o["event"] for o in main["bd_outputs"])),
           "compact": {"measured": False, "why": "not tried"}, "session_tokens": run_tokens(main)}
    run["tokens"]["input"] = sum(run["per_call_input"])
    run["peak_input"] = max(run["per_call_input"] or [0])
    if not sid or main["rc"] != 0 or res.get("is_error"):
        run["compact"] = {"measured": False, "why": "main run failed"}
        return run
    if budget.used >= budget.cap:
        run["compact"] = {"measured": False, "why": "token cap reached before the compact leg"}
        budget.stopped = True
        return run
    c1 = call(["--resume", sid, "/compact"])
    budget.note(sid, run_tokens(c1))
    c2 = call(["--resume", sid, "reply ok"])
    budget.note(sid, run_tokens(c2))
    run["session_tokens"] += run_tokens(c1) + run_tokens(c2)
    run["subagent_stop"] = sum(x["hooks_started"].get("SubagentStop", 0) for x in (main, c1, c2))
    floor = total_input(c2["calls"][0]) if c2["calls"] else 0
    if c1["compact"] and floor:
        run["compact"] = {"measured": True, "pre_tokens": c1["compact"].get("pre_tokens"),
                          "post_tokens": c1["compact"].get("post_tokens"), "compact_ms": c1["compact"].get("duration_ms"),
                          "floor_input": floor, "slim": any("PreCompact" in o["event"] for o in c1["bd_outputs"]),
                          "bd_outputs": len(c1["bd_outputs"]) + len(c2["bd_outputs"]),
                          "wall_ms": c1["wall_ms"] + c2["wall_ms"]}
    else:
        why = "no compact_boundary in the /compact run" if not c1["compact"] else "no call after the compact"
        run["compact"] = {"measured": False, "why": why, "floor_input": floor}
    return run


# ----------------------------------------------------------------------------------------------- overhead + latency
def count_files(root):
    out = {}
    for group, pats in (("hooks", HOOK_FILES), ("scripts", SCRIPT_FILES)):
        files = sorted(p for pat in pats for p in glob.glob(os.path.join(root, *pat.split("/"))))
        lines = 0
        for p in files:
            with open(p, "rb") as f:
                lines += sum(1 for _ in f)
        out[group] = {"files": len(files), "lines": lines}
    out["total"] = {"files": sum(v["files"] for v in out.values()), "lines": sum(v["lines"] for v in out.values())}
    try:
        with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8", errors="replace") as f:
            hooks = json.load(f).get("hooks", {})
    except (OSError, ValueError):
        hooks = {}
    out["hook_events"] = sorted(hooks)
    out["hook_commands"] = sum(len(h.get("hooks", [])) for ev in hooks.values() for h in ev)
    return out


def canned_events(tmp, transcript):
    notes = os.path.join(tmp, "notes.md")
    base = {"session_id": "versus", "transcript_path": transcript, "cwd": tmp}
    return [
        ("UserPromptSubmit", "run.sh", "context_guard.py", dict(base, hook_event_name="UserPromptSubmit", prompt="reply ok")),
        ("PreToolUse gate (stop.sh, off)", "stop.sh", None, dict(base, hook_event_name="PreToolUse", tool_name="Bash",
                                                                 tool_input={"command": "echo hi"})),
        ("PreToolUse:Read (read guard)", "run.sh", "read_guard.py", dict(base, hook_event_name="PreToolUse", tool_name="Read",
                                                                         tool_input={"file_path": notes})),
        ("PostToolUse (guard+watch+loop)", "run.sh", "post_tool.py", dict(base, hook_event_name="PostToolUse", tool_name="Read",
                                                                         tool_input={"file_path": notes},
                                                                         tool_response={"file": {"content": "x" * 2000}})),
        ("PreCompact (reset+slim)", "run.sh", "pre_compact.py", dict(base, hook_event_name="PreCompact", trigger="manual",
                                                                   custom_instructions="")),
        ("SessionStart", "run.sh", "session_start.py", dict(base, hook_event_name="SessionStart", source="startup")),
        ("SubagentStart (monitor)", "run.sh", "agent_monitor.py", dict(base, hook_event_name="SubagentStart", agent_id="a1",
                                                                      agent_type="general-purpose")),
    ]


def measure_latency(root, reps=30):
    """Each hook entry point timed `reps` times on a canned event (steady state: the state files persist between
    reps, as in a real chat), under a temp HOME. Median and p95 in ms. Also the bare `sh` start for scale."""
    sh = shutil.which("sh")
    if not sh:
        return {"error": "no sh on PATH"}
    tmp = tempfile.mkdtemp(prefix="bd-versus-lat-")
    try:
        home = os.path.join(tmp, "home")
        cfg = os.path.join(home, ".claude")
        bd = os.path.join(cfg, "bigdad")
        state = os.path.join(tmp, "state")
        for d in (bd, state, os.path.join(cfg, "projects", "x")):
            os.makedirs(d)
        with open(os.path.join(bd, "baseline.json"), "w", encoding="utf-8") as f:     # a shown, fresh baseline: no background build
            json.dump({"ts": "2026-01-01T00:00:00", "guide": GUARD_AT, "chats": 1, "logs": 1, "shown": True, "calls": 1,
                       "sent": 1, "resent_share": 0.5, "startup_median": 1, "peak": 1, "past_guide": 0, "above_guide": 0}, f)
        transcript = os.path.join(cfg, "projects", "x", "versus.jsonl")
        with open(transcript, "w", encoding="utf-8") as f:
            for i in range(3):
                f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {
                    "id": f"m{i}", "model": "claude-haiku-5-5", "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 100, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 30000,
                              "output_tokens": 10}}}) + "\n")
        with open(os.path.join(tmp, "notes.md"), "w") as f:
            f.write("x\n" * 100)
        env = {k: v for k, v in os.environ.items() if not k.startswith("BIGDAD_")}
        env.update(HOME=home, USERPROFILE=home, CLAUDE_CONFIG_DIR=cfg, BIGDAD_STATE_DIR=state, BIGDAD_GUARD_AT=str(GUARD_AT),
                   BIGDAD_LEARN="off", PYTHONIOENCODING="utf-8", CLAUDE_PLUGIN_ROOT=root)   # timed acting, with or without --learning
        out = {"reps": reps, "hooks": {}}

        def timed(cmd, data):
            t0 = time.perf_counter()
            subprocess.run(cmd, input=data, capture_output=True, env=env, cwd=tmp, timeout=60)
            return (time.perf_counter() - t0) * 1000

        samples = [timed([sh, "-c", "exit 0"], b"") for _ in range(reps)]
        out["sh_start_ms"] = {"median": round(statistics.median(samples), 1), "p95": round(p95(samples), 1)}
        for label, wrapper, script, event in canned_events(tmp, transcript):
            cmd = [sh, os.path.join(root, "hooks", wrapper)] + ([script] if script else [])
            data = json.dumps(event).encode("utf-8")
            samples = [timed(cmd, data) for _ in range(reps)]
            out["hooks"][label] = {"median": round(statistics.median(samples), 1), "p95": round(p95(samples), 1)}
        h = out["hooks"]
        per = lambda *ks: round(sum(h[k]["median"] for k in ks if k in h), 1)
        out["per_read_call_ms"] = per("PreToolUse gate (stop.sh, off)", "PreToolUse:Read (read guard)", "PostToolUse (guard+watch+loop)")
        out["per_bash_call_ms"] = per("PreToolUse gate (stop.sh, off)", "PostToolUse (guard+watch+loop)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def p95(xs):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def observational(transcript, reps=3):
    """The build chat's own receipt: plain compact drops vs slim compact drops (different content: observational),
    and how long `bigdad.py receipt` takes on that log."""
    if not transcript or not os.path.isfile(transcript):
        return {"measured": False, "why": "no transcript given" if not transcript else "transcript not found"}
    import bd_money as M
    t = M.read(transcript, guide=130_000)
    groups = {"plain": [], "slim": []}
    for ts, label, before, after in t.cuts:
        if "compact" not in label:
            continue
        groups["slim" if label.startswith("slim ") else "plain"].append((before, after))
    summary = {}
    for k, rows in groups.items():
        if rows:
            summary[k] = {"n": len(rows), "mean_drop_pct": round(statistics.mean(100 * (b - a) / b for b, a in rows), 1),
                          "mean_before": int(statistics.mean(b for b, _ in rows)),
                          "mean_after": int(statistics.mean(a for _, a in rows))}
        else:
            summary[k] = {"n": 0}
    tmp = tempfile.mkdtemp(prefix="bd-versus-rc-")
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("BIGDAD_")}
        env.update(HOME=tmp, USERPROFILE=tmp, CLAUDE_CONFIG_DIR=os.path.join(tmp, ".claude"), NO_COLOR="1", PYTHONIOENCODING="utf-8")
        times = []
        for _ in range(reps):
            t0 = time.perf_counter()
            subprocess.run([sys.executable, os.path.join(HERE, "bigdad.py"), "receipt", transcript], capture_output=True, env=env,
                           timeout=300)
            times.append((time.perf_counter() - t0) * 1000)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"measured": True, "calls": t.calls, "compacts": t.compacts, "slims": t.slims, "size_mb": round(os.path.getsize(transcript) / 1e6, 1),
            "drops": summary, "receipt_ms": {"median": int(statistics.median(times)), "runs": reps},
            "blocks": t.blocks, "warns": t.warns, "pauses": t.pauses}


def realdir_snapshot():
    """Big Dad's own outputs in the real ~/.claude/bigdad (not live.json, not versus/): name -> (size, mtime)."""
    import bd_common as C
    out = {}
    for pat in ("*.jsonl", "*.json", "*.html", "python-cmd"):
        for p in glob.glob(os.path.join(C.bd_dir(), pat)):
            if os.path.basename(p) == "live.json":
                continue
            try:
                st = os.stat(p)
                with open(p, "rb") as f:
                    lines = sum(1 for _ in f)
                out[os.path.basename(p)] = (st.st_size, st.st_mtime, lines)
            except OSError:
                pass
    return out


def realdir_diff(before, after):
    """['agents.jsonl (+3 lines)', ...] for every file whose size/mtime/line count moved."""
    out = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            d = (after.get(k) or (0, 0, 0))[2] - (before.get(k) or (0, 0, 0))[2]
            out.append(f"{k} ({d:+d} lines)" if d else f"{k} (rewritten)")
    return out


def new_agent_rows(before):
    """agent_type of each agents.jsonl row added during the test. A real type means another live session started or
    stopped a real agent meanwhile; '?' means a stop with no agent (the old phantom-row bug)."""
    import bd_common as C
    skip = (before.get("agents.jsonl") or (0, 0, 0))[2]
    rows = []
    try:
        with open(os.path.join(C.bd_dir(), "agents.jsonl"), encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= skip:
                    try:
                        rows.append(str(json.loads(line).get("agent_type") or "?"))
                    except ValueError:
                        rows.append("?")
    except OSError:
        pass
    return rows


def claude_version():
    try:
        return subprocess.run([shutil.which("claude") or "claude", "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "?"


# ----------------------------------------------------------------------------------------------- run command
def versus_root(flag=""):
    """--root, else BIGDAD_VERSUS_ROOT, else DEFAULT_ROOT (C:/bdversus on Windows, /tmp/bdversus elsewhere)."""
    return os.path.abspath(flag or os.environ.get("BIGDAD_VERSUS_ROOT") or DEFAULT_ROOT)


def real_sessions(sids):
    """Versus sessions found in the real config dir's projects (must be 0: every run has its own config dir)."""
    dirs = {os.path.join(I.real_home(), ".claude")} | ({os.environ["CLAUDE_CONFIG_DIR"]} if os.environ.get("CLAUDE_CONFIG_DIR") else set())
    return sum(len(glob.glob(os.path.join(d, "projects", "*", f"{sid}.jsonl"))) for d in dirs for sid in sids if sid)


def cmd_run(args):
    runs, out, transcript, commit, cap = 3, os.path.join(os.getcwd(), "versus-out"), "", COMMIT, CAP
    keep_raw, root, bd_state = False, "", "acting"
    it = iter(args)
    for a in it:
        if a == "--runs":
            runs = int(next(it))
        elif a == "--out":
            out = os.path.abspath(next(it))
        elif a == "--transcript":
            transcript = next(it)
        elif a == "--commit":
            commit = next(it)
        elif a == "--cap":
            kind, cap = C.parse_cap(next(it))
            if kind != "tokens":
                sys.exit("versus run: --cap takes tokens, e.g. 4m or 4000000")
        elif a == "--keep-raw":
            keep_raw = True
        elif a == "--learning":
            bd_state = "learning"
        elif a == "--root":
            root = next(it)
        else:
            sys.exit(f"versus run: unknown option {a}")
    root = versus_root(root)
    bad = I.check_root(root)
    if bad:
        sys.exit("versus run: root not isolated (pick one outside your home folder with --root or BIGDAD_VERSUS_ROOT):\n  "
                 + "\n  ".join(bad))
    try:
        auth = I.auth_block()
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"versus run: no usable login: {e}")
    os.makedirs(out, exist_ok=True)
    made_root = not os.path.isdir(root)
    os.makedirs(root, exist_ok=True)
    if made_root:
        try:
            I.make_private(root, folder=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            print(f"versus run: warning, the run folder could not be made private: {e}", flush=True)
    stale = I.sweep_logins(root)             # a run killed hard last time left its login copy: gone before anything
    if stale:
        print(f"versus run: deleted {len(stale)} login copies an earlier killed run left under {root}", flush=True)
    work = tempfile.mkdtemp(prefix="versus-", dir=root)
    say = lambda s: print(s, flush=True)
    say(f"🥛 Big Dad versus stock Claude Code: {runs} runs per arm, cap {big(cap)} tokens, root {root}, out {out}")
    say(f"  B state: {STATE_TEXT[bd_state][1]}")
    before = realdir_snapshot()
    snap = os.path.join(work, "snapshot")
    ctx = {"root": work, "auth": auth, "baseline": None, "markers": I.markers(), "sids": [], "bd_state": bd_state}
    data = {"version": 1, "spec": SPEC, "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "commit": commit,
            "params": {"model": MODEL, "guard_at": GUARD_AT, "max_turns": MAX_TURNS, "allowed_tools": ALLOWED, "runs_per_arm": runs,
                       "cap_tokens": cap, "seed": SEED, "prompt": PROMPT, "isolation": ISOLATION, "flags": list(I.FLAGS),
                       "bigdad_state": bd_state, "env": "allow-list, own temp", "login": "access token only",
                       "root": root, "order": "N,B interleaved",
                       "claude_code": claude_version(), "python": platform.python_version(), "os": platform.system()},
            "runs": [], "stopped_at_cap": False}
    try:
        os.makedirs(snap)
        data["commit_full"] = snapshot(commit, snap)
        raw_dir = os.path.join(out, "raw") if keep_raw else None
        if raw_dir:
            os.makedirs(raw_dir, exist_ok=True)
        budget = Budget(cap)
        for i in range(1, runs + 1):
            for arm in ("N", "B"):
                if budget.used >= cap:
                    data["stopped_at_cap"] = True
                    say(f"  token cap {big(cap)} reached at {big(budget.used)}: stopping")
                    break
                try:
                    ctx["auth"] = I.auth_block()        # re-read each run: never let a run refresh the login in a temp dir
                except (OSError, ValueError, RuntimeError) as e:
                    data["stopped"] = f"login: {e}"
                    say(f"  stopping before run {i} {arm}: {e}")
                    break
                r = one_run(i, arm, snap, budget, say, raw_dir, ctx)
                data["runs"].append(r)
                c = r["compact"]
                say(f"  run {i} {arm}  wall {r['wall_ms'] / 1000:.1f}s  tokens {big(r['session_tokens'])}  input {r['tokens']['input'] / 1000:.0f}k"
                    f"  calls {r['calls']}  turns {r['turns']}  bd-outputs {r['bd_outputs']}  denied {r['denied_reads']}"
                    f"  floor {c.get('floor_input', 0) / 1000:.0f}k{'' if c.get('measured') else ' (' + c.get('why', '') + ')'}"
                    f"  used {big(budget.used)}  {'stock' if r['clean'] else 'ISOLATION: ' + '; '.join(r['isolation'])}")
                if budget.stopped:
                    data["stopped_at_cap"] = True
            if data["stopped_at_cap"] or data.get("stopped"):
                break
        data["used_tokens"] = budget.used
        say("  overhead: files, hooks, latency (30x each hook)...")
        data["overhead"] = count_files(snap)
        data["latency"] = measure_latency(snap)
        say("  observational: the build chat receipt...")
        data["observational"] = observational(transcript)
        data["native"] = {"tools": NATIVE, "lacks": list(NATIVE_LACKS)}
    finally:
        left = no_auth_left(work)
        stuck = I.logins_stuck(left)
        if stuck:
            say(f"versus run: WARNING, {len(stuck)} login copies could not be deleted: {stuck}")
        rm_tree(work)
        if made_root:
            try:
                os.rmdir(root)
            except OSError:
                pass
    data["isolation"] = {"mode": ISOLATION, "baseline_skills": len(ctx["baseline"] or []),
                         "markers_checked": len(ctx["markers"][0]) + len(ctx["markers"][1]),
                         "problem_runs": [f"{r['i']}{r['arm']}" for r in data["runs"] if not r.get("clean")],
                         "real_projects_sessions": real_sessions(ctx["sids"]), "login_files_left": len(left),
                         "login_files_stuck": len(stuck)}
    after = realdir_snapshot()
    changed = realdir_diff(before, after)
    data["realdir"] = {"untouched": not changed, "changed": changed, "agent_rows": new_agent_rows(before)}
    path = os.path.join(out, "versus-runs.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(scrub(json.dumps(data, indent=1, ensure_ascii=False)))
    say(f"  saved {path}")
    cmd_receipt([out])


# ----------------------------------------------------------------------------------------------- receipt
def scrub(text):
    """No home path, user name, session id (UUID) or email leaves this module."""
    home = os.path.expanduser("~")
    for h in {home, home.replace(chr(92), "/"), home.replace("/", chr(92)), home.replace(chr(92), chr(92) * 2)}:
        if h:
            text = re.sub(re.escape(h), "~", text, flags=re.I)
    user = os.path.basename(home) or os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if user and len(user) > 2:
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", "~", text, flags=re.I)
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text, flags=re.I)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"sk-ant-[A-Za-z0-9_-]{8,}", "<token>", text)          # a token a run's shell printed
    return text


def session_tokens(r):
    """Input + output of one run's session, compact legs included (old records: the main run's tokens)."""
    v = r.get("session_tokens")
    return v if isinstance(v, int) else r["tokens"]["input"] + r["tokens"]["out"]


def used(d):
    v = d.get("used_tokens")
    return big(v) if isinstance(v, int) else big(sum(session_tokens(r) for r in d.get("runs") or []))


def cap_of(p):
    v = p.get("cap_tokens")
    return big(v if isinstance(v, int) else CAP)


def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else 0


def big(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else f"{int(n)}"


def delta(n, b):
    if not n:
        return "n/a" if not b else "new"
    return f"{100 * (b - n) / n:+.0f}%"


class Side:
    """One arm's medians over its runs."""

    def __init__(self, runs):
        self.runs = runs
        self.n = len(runs)
        tok = lambda k: med([r["tokens"][k] for r in runs])
        self.read, self.w5, self.w1h, self.new, self.out, self.input = (tok(k) for k in ("read", "w5", "w1h", "new", "out", "input"))
        self.written = self.w5 + self.w1h
        self.session_tokens = med([session_tokens(r) for r in runs])
        self.calls, self.turns = med([r["calls"] for r in runs]), med([r["turns"] for r in runs])
        self.peak = med([r["peak_input"] for r in runs])
        self.wall = med([r["wall_ms"] for r in runs])
        self.duration = med([r["duration_ms"] for r in runs])
        self.api = med([r["duration_api_ms"] for r in runs])
        self.denied = sum(r["denied_reads"] for r in runs)
        self.notes = sum(r["bd_outputs"] for r in runs)
        self.note_tokens = med([r["bd_note_chars"] // 4 for r in runs])
        self.briefs = [r["bd_briefs"] for r in runs]
        self.outputs = [r["bd_outputs"] for r in runs]
        self.correct = sum(1 for r in runs if r.get("answer_ok"))
        self.sub_stops = sum(r.get("subagent_stop", 0) for r in runs)
        floors = [r["compact"]["floor_input"] for r in runs if r["compact"].get("measured")]
        self.floor = med(floors)
        self.floor_n = len(floors)
        self.compact_ms = med([r["compact"].get("compact_ms") for r in runs if r["compact"].get("measured")])
        self.pre = med([r["compact"].get("pre_tokens") for r in runs if r["compact"].get("measured")])
        self.post = med([r["compact"].get("post_tokens") for r in runs if r["compact"].get("measured")])
        self.slim = sum(1 for r in runs if r["compact"].get("slim"))
        self.errors = sum(1 for r in runs if r.get("is_error") or r.get("rc"))
        self.compact_why = sorted(set(r["compact"].get("why", "") for r in runs if not r["compact"].get("measured")))


# Big Dad's state in B, for the receipt: (markdown, terminal, env line part)
STATE_TEXT = {
    "acting": ("acting (`BIGDAD_LEARN=off`: it blocks, shrinks and notes from the first call). A fresh install learns "
               "first; `versus run --learning` measures that state.",
               "acting (BIGDAD_LEARN=off)", "; B only: `BIGDAD_LEARN=off` (acting)"),
    "learning": ("learning (a new install's learning period: its hooks only count; nothing is blocked, shrunk or "
                 "noted). `versus run` without `--learning` measures Big Dad acting.",
                 "learning (new install: hooks only count)", "; B only: no `BIGDAD_LEARN` (left in its learning period)"),
    None: ("not recorded (run made before 2.0.7; a 2.0.6 or later Big Dad with a fresh state folder ran in its "
           "learning period, earlier versions acted at once).", "not recorded (run before 2.0.7)", ""),
}


def bd_state(p):
    """params["bigdad_state"] when it is one of STATES, else None (a record from before 2.0.7)."""
    v = p.get("bigdad_state")
    return v if v in STATES else None


def run_problems(r, p):
    """One run's recorded problems, plus a state mismatch the record shows (older harness: not recorded at run time)."""
    bad = list(r.get("isolation") or [])
    extra = state_problem(r.get("arm"), r.get("bd_state_seen"), bd_state(p))
    return bad + [extra] if extra and extra not in bad else bad


def verdict(N, B, d, lat):
    """Plain sentences with the numbers; wins go to whoever has them. While B was learning, what Big Dad does only
    when acting is listed as not exercised, never as a win."""
    win, nat, wash, skip = [], [], [], []
    def place(name, n, b, lower_is_better=True, unit=big):
        if not n and not b:
            return
        pct = (b - n) / n if n else 0
        line = f"{name}: N {unit(n)} vs B {unit(b)} ({delta(n, b)})"
        better = pct < -0.05 if lower_is_better else pct > 0.05
        worse = pct > 0.05 if lower_is_better else pct < -0.05
        (win if better else nat if worse else wash).append(line)
    place("total input re-sent per run", N.input, B.input)
    place("cache re-reads per run", N.read, B.read)
    place("tokens per run (input + output, incl. compact leg)", N.session_tokens, B.session_tokens)
    place("peak call size", N.peak, B.peak)
    if N.floor_n and B.floor_n:
        place("post-compact floor", N.floor, B.floor)
    place("wall time per run", N.wall, B.wall, unit=lambda x: f"{x / 1000:.1f}s")
    place("output tokens per run", N.out, B.out)
    if B.denied:
        win.append(f"double reads blocked: {B.denied} over {B.n} runs (native: 0, the Read tool re-sends the file)")
    if N.n and B.n and N.correct != B.correct:
        side = win if B.correct > N.correct else nat
        side.append(f"answer accuracy: N {N.correct}/{N.n} vs B {B.correct}/{B.n} right (both line counts); see per-run answers in versus-runs.json")
    elif N.n and B.n:
        wash.append(f"answer accuracy: N {N.correct}/{N.n} vs B {B.correct}/{B.n} right (both line counts)")
    if B.notes:
        nat.append(f"Big Dad's own words add tokens: ~{int(B.note_tokens)} tokens of notes per run (est. chars/4) over "
                   f"{int(med(B.outputs))} hook outputs; native injects none")
    oh = d.get("overhead") or {}
    if oh:
        nat.append(f"management overhead: native 0 files, 0 hooks, 0 setup steps; Big Dad {oh['total']['files']} files, "
                   f"{oh['total']['lines']:,} lines, {oh['hook_commands']} hook commands on {len(oh['hook_events'])} events, 1 install step")
    if lat and "per_read_call_ms" in lat:
        nat.append(f"hook latency: +{lat['per_read_call_ms']:.0f} ms per Read call, +{lat['per_bash_call_ms']:.0f} ms per Bash call "
                   f"(median, steady state; sh start alone {lat['sh_start_ms']['median']:.0f} ms)")
    ob = d.get("observational") or {}
    if ob.get("measured") and ob["drops"].get("slim", {}).get("n") and ob["drops"].get("plain", {}).get("n"):
        s, p = ob["drops"]["slim"], ob["drops"]["plain"]
        win.append(f"slim compacts on the build chat dropped {s['mean_drop_pct']:.0f}% per call vs {p['mean_drop_pct']:.0f}% for plain "
                   f"compacts ({s['n']} vs {p['n']}; observational, different content each time)")
    if bd_state(d.get("params") or {}) == "learning":
        skip.append("not exercised in this state: the read block, the output shrink, the slim compact and Big Dad's "
                    "notes (B was in its learning period: its hooks only counted; nothing was blocked, shrunk or noted)")
        skip.append("native lacks these too, but this run did not exercise them: " + "; ".join(NATIVE_LACKS)
                    + "; rerun without --learning to measure Big Dad acting")
    else:
        win.append("what native has no answer for: " + "; ".join(NATIVE_LACKS))
    return win, nat, wash, skip


def table(rows, cols):
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" if i == 0 else "---:" for i in range(len(cols))) + "|"
    return "\n".join([head, sep] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def stock_run(p):
    """True for runs made since 1.7.0 (N = stock Claude Code, isolated). Older versus-runs.json files carry
    params["settings"] instead: their N arm was the owner's own setup with only the Big Dad plugin turned off."""
    return p.get("isolation") == ISOLATION


def isolation_text(runs, p=None):
    """(problem runs, one line) for the receipt: every run's recorded isolation check and B state check."""
    p = p or {}
    bad = [r for r in runs if run_problems(r, p)]
    if not bad:
        return bad, f"Isolation checked on every run: 0 of {len(runs)} runs had a problem"
    return bad, (f"Isolation problems in {len(bad)} of {len(runs)} runs: "
                 + "; ".join(f"run {r['i']}{r['arm']}: {', '.join(run_problems(r, p))}" for r in bad))


def build_receipt(d):
    """(markdown, terminal text) from the runs JSON dict."""
    runs = d.get("runs") or []
    N, B = Side([r for r in runs if r["arm"] == "N"]), Side([r for r in runs if r["arm"] == "B"])
    lat, oh, ob = d.get("latency") or {}, d.get("overhead") or {}, d.get("observational") or {}
    p = d.get("params") or {}
    stock = stock_run(p)
    ncol = "N stock" if stock else "N your setup"
    bad_runs, iso_line = isolation_text(runs, p)
    state = bd_state(p)
    if stock:
        n_arm = ("- N arm: stock Claude Code, nothing of your setup: its own config dir, home and working folder per run "
                 f"under `{p.get('root', DEFAULT_ROOT)}` (outside your home folder, deleted after), no settings files, no MCP "
                 "servers, none of your plugins, skills, agents, CLAUDE.md or MCP servers; B is the same plus Big Dad. "
                 + iso_line + ".")
        flags = " ".join(x if x else '""' for x in p.get("flags") or I.FLAGS)
        cmd_line = (f"- command: `claude -p --model {p.get('model', MODEL)} --output-format stream-json --verbose --include-hook-events "
                    f"{flags} --allowedTools \"{p.get('allowed_tools', ALLOWED)}\" --max-turns {p.get('max_turns', MAX_TURNS)}` "
                    "(+ `--plugin-dir <snapshot>` in B only)")
        env_line = ("- env, both arms: fresh `CLAUDE_CONFIG_DIR`, `HOME`, `USERPROFILE` (and `HOMEDRIVE`/`HOMEPATH`/`APPDATA`/`LOCALAPPDATA`) "
                    "inside the run folder; "
                    + (("from the host only an allow-list (PATH, system folders, locale, proxy and CA settings; "
                        "temp is the run's own)") if p.get("env") == "allow-list, own temp" else
                       "from the host only an allow-list (PATH, system folders, TEMP/TMP, locale, proxy and CA settings)"
                       if p.get("env") == "allow-list" else
                       "every `CLAUDE*`, `ANTHROPIC*`, `BIGDAD*`, `MCP*`, `AI_AGENT` variable dropped")
                    + f"; then `BIGDAD_GUARD_AT={p.get('guard_at', GUARD_AT)}`, `BIGDAD_STATE_DIR=<run folder>`, `PYTHONIOENCODING=utf-8`"
                    + STATE_TEXT[state][2] + "; login: only "
                    + ("the `claudeAiOauth` access token (no refresh token)" if p.get("login") == "access token only"
                       else "the `claudeAiOauth` block") + ", copied in for each call and deleted after")
    else:
        n_arm = ("- N arm: NOT stock Claude Code. This run predates 1.7.0: N was your own Claude Code setup with only the Big Dad "
                 "plugin turned off (your other plugins, CLAUDE.md and MCP servers loaded in both arms). Rerun `versus run` for a stock N.")
        cmd_line = (f"- command: `claude -p --model {p.get('model', MODEL)} --output-format stream-json --verbose --include-hook-events "
                    f"--allowedTools \"{p.get('allowed_tools', ALLOWED)}\" --max-turns {p.get('max_turns', MAX_TURNS)} "
                    f"--settings '{p.get('settings', '?')}'` (+ `--plugin-dir <snapshot>` in B only)")
        env_line = f"- env, both arms: `BIGDAD_GUARD_AT={p.get('guard_at', GUARD_AT)}`, `PYTHONIOENCODING=utf-8`, every other `BIGDAD_*` unset"
    model = next((r.get("model") for r in runs if r.get("model")), p.get("model", "?"))
    win, nat, wash, skip = verdict(N, B, d, lat)
    ms = lambda x: f"{x / 1000:.1f} s" if x else "n/a"
    est = lambda x: f"~{int(x)}" if x else "0"
    floor = lambda S: big(S.floor) if S.floor_n else "not measured"
    # ---- token table
    tok_rows = [
        ("total input per run (new+written+read)", big(N.input), big(B.input), delta(N.input, B.input), "measured"),
        ("cache read", big(N.read), big(B.read), delta(N.read, B.read), "measured"),
        ("cache write (5 min + 1 h)", big(N.written), big(B.written), delta(N.written, B.written), "measured"),
        ("new input", big(N.new), big(B.new), delta(N.new, B.new), "measured"),
        ("output", big(N.out), big(B.out), delta(N.out, B.out), "measured"),
        ("peak call size", big(N.peak), big(B.peak), delta(N.peak, B.peak), "measured"),
        ("API calls / turns", f"{N.calls:g} / {N.turns:g}", f"{B.calls:g} / {B.turns:g}", delta(N.calls, B.calls), "measured"),
        ("tokens per run, with compact leg (input + output)", big(N.session_tokens), big(B.session_tokens),
         delta(N.session_tokens, B.session_tokens), "measured"),
        ("post-compact floor (input of `reply ok`)", floor(N), floor(B), delta(N.floor, B.floor) if N.floor_n and B.floor_n else "n/a", "measured"),
        ("compact: pre -> post tokens (Claude Code's)", f"{big(N.pre)} -> {big(N.post)}" if N.floor_n else "n/a",
         f"{big(B.pre)} -> {big(B.post)}" if B.floor_n else "n/a", "", "measured"),
        ("double reads blocked (all runs)", N.denied, B.denied, "", "counted"),
        ("slim compact text used by /compact (runs)", f"{N.slim}/{N.n}", f"{B.slim}/{B.n}", "", "counted"),
        ("Big Dad hook outputs (all runs)", N.notes, B.notes, "", "counted"),
        ("Big Dad note tokens per run", est(N.note_tokens), est(B.note_tokens), "", "estimated (chars/4)"),
        ("correct answers (both line counts right)", f"{N.correct}/{N.n}", f"{B.correct}/{B.n}", "", "checked against the fixture"),
    ]
    # ---- overhead table
    oh_rows = [("files (hooks + scripts + skill)", 0, oh.get("total", {}).get("files", "?"), "counted"),
               ("lines", 0, f"{oh.get('total', {}).get('lines', 0):,}", "counted"),
               ("hook commands registered", 0, f"{oh.get('hook_commands', '?')} on {len(oh.get('hook_events', []))} events", "counted"),
               ("setup steps", "0 (built in)", "1 (install/enable the plugin); optional: `bigdad.py setup`, status line", "counted"),
               ("tokens injected at SessionStart", 0, est(med([r['bd_note_chars'] // 4 for r in B.runs]) if B.runs else 0) + " total per run incl. notes", "estimated (chars/4)")]
    if lat and "hooks" in lat:
        for label, v in lat["hooks"].items():
            oh_rows.append((f"hook latency: {label}", "0 ms", f"{v['median']:.0f} ms (p95 {v['p95']:.0f})", f"measured, {lat['reps']}x"))
        oh_rows.append(("hook ms per Read call (gate + read guard + post)", "0 ms", f"{lat['per_read_call_ms']:.0f} ms", "computed from medians"))
        oh_rows.append(("hook ms per Bash call (gate + post)", "0 ms", f"{lat['per_bash_call_ms']:.0f} ms", "computed from medians"))
        oh_rows.append(("bare `sh -c exit 0` (scale)", "", f"{lat['sh_start_ms']['median']:.0f} ms (p95 {lat['sh_start_ms']['p95']:.0f})", "measured"))
    # ---- runtime table
    rt_rows = [("wall time per run (process clock)", ms(N.wall), ms(B.wall), delta(N.wall, B.wall), "measured, median"),
               ("duration_ms (Claude Code's)", ms(N.duration), ms(B.duration), delta(N.duration, B.duration), "measured, median"),
               ("duration_api_ms", ms(N.api), ms(B.api), delta(N.api, B.api), "measured, median"),
               ("compact duration_ms", ms(N.compact_ms) if N.floor_n else "n/a", ms(B.compact_ms) if B.floor_n else "n/a",
                delta(N.compact_ms, B.compact_ms) if N.floor_n and B.floor_n else "n/a", "measured"),
               ("hook ms per tool call", "0", f"{lat.get('per_read_call_ms', 0):.0f} (Read) / {lat.get('per_bash_call_ms', 0):.0f} (Bash)", "", "measured"),
               ("receipt command on the build chat log", "n/a (no native equivalent)",
                f"{ob['receipt_ms']['median'] / 1000:.1f} s ({ob['size_mb']} MB, {ob['calls']} calls)" if ob.get("measured") else "not measured", "", "measured")]
    # ---- per-run table
    run_rows = [(r["i"], r["arm"], big(r["tokens"]["input"]), big(r["tokens"]["read"]), big(r["tokens"]["w5"] + r["tokens"]["w1h"]),
                 big(r["tokens"]["out"]), r["calls"], big(session_tokens(r)), ms(r["wall_ms"]), r["denied_reads"], r["bd_outputs"],
                 big(r["compact"]["floor_input"]) if r["compact"].get("measured") else "n/m", r.get("subtype") or ("error" if r.get("is_error") else "ok"))
                for r in runs]
    proof_rows = [(r["i"], r["arm"], r["bd_briefs"], r["bd_outputs"], ", ".join(f"{k}={v}" for k, v in sorted(r["hooks_started"].items())) or "none",
                   "; ".join(run_problems(r, p)) if run_problems(r, p) else
                   ("stock" if r["arm"] == "N" else "stock + Big Dad") if r.get("clean") else "not checked")
                  for r in runs]
    rd = d.get("realdir") or {}
    rd_text = "yes" if rd.get("untouched") else "NO: " + ", ".join(rd.get("changed", []))
    rd_note = None
    rows = rd.get("agent_rows")
    touched_agents = not rd.get("untouched") and any(c.startswith("agents.jsonl") for c in rd.get("changed", []))
    if touched_agents and rows and all(t != "?" for t in rows):
        rd_note = (f"Real-dir write: {len(rows)} `agents.jsonl` row(s) for real agents ({', '.join(sorted(set(rows)))}) that another live "
                   "session started or stopped during the test; not written by the runs (no '?' phantom rows).")
    elif touched_agents and B.sub_stops:
        rd_note = (f"Real-dir write: Big Dad's agent monitor appended to `~/.claude/bigdad/agents.jsonl` on a SubagentStop event that headless "
                   f"`claude -p --resume` fires with no subagent (B {B.sub_stops}, N {N.sub_stops} such events; the test spawned no agents; "
                   f"rows carry agent_type '?'). That is a Big Dad bug to fix, not a harness write; `BIGDAD_AGENTS=off` would hide it but the spec keeps the env clean.")
    limits = [f"Headless /compact: {'worked in every run (compact_boundary logged)' if all(r['compact'].get('measured') for r in runs) else 'not measured in some runs: ' + '; '.join(sorted(set(w for S in (N, B) for w in S.compact_why)))}.",
              "Test scale: the guide is 20k so Big Dad's notes fire; the system prompt plus tools are a fixed load on every call, so the post-compact floor is mostly that load.",
              "Big Dad's note tokens are estimated (chars/4): Claude Code does not report hook text tokens separately.",
              "Hook latency is the hook process alone (sh + Python) on canned events, steady state; Claude Code's own hook dispatch is not included."
              + (" It is timed with Big Dad acting, not in its learning period." if state == "learning" else ""),
              "The build-chat compact comparison is observational: plain and slim compacts summarised different content at different times.",
              ("N ran no hooks of any kind (a hook in N counts as an isolation problem); B ran Big Dad's hooks only. " + iso_line + "."
               if stock else "Other hooks installed on this machine (if any) ran in both arms alike; see the proof table. "
               "N was your setup without Big Dad, not stock Claude Code (run made before 1.7.0)."),
              f"Stopped at the token cap: {'yes' if d.get('stopped_at_cap') else 'no'}; used {used(d)} of {cap_of(p)} tokens.",
              "Tokens are Claude Code's own `usage` rows; answers are checked against the fixture's real line counts."] + ([rd_note] if rd_note else [])
    md = [f"# 🥛 Big Dad vs {'stock Claude Code' if stock else 'your setup without Big Dad'} · VERSUS RECEIPT 🧾", "",
          f"- date: {d.get('date', '?')}", f"- Big Dad under test: commit `{d.get('commit', '?')}` ({(d.get('commit_full') or '?')[:12]}), loaded once via `--plugin-dir` from a `git archive` snapshot",
          f"- Big Dad state in B: {STATE_TEXT[state][0]}",
          f"- model: `{model}` · Claude Code {p.get('claude_code', '?')} · Python {p.get('python', '?')} on {p.get('os', '?')}",
          f"- runs: N {N.n} · B {B.n}, interleaved N,B · tokens used {used(d)} (cap {cap_of(p)})",
          f"- spec: {d.get('spec', SPEC)}", n_arm, "",
          "## Parameters", "",
          cmd_line,
          env_line,
          f"- fixture (seed {p.get('seed', SEED)}): " + ", ".join(f"{k} {v['lines']} lines / {v['bytes']:,} bytes (sha256 {v['sha256']}...)" for k, v in (runs[0]["fixture"].items() if runs else [])),
          f"- prompt: {p.get('prompt', PROMPT)}",
          "- compaction leg: `--resume <id> \"/compact\"` then `--resume <id> \"reply ok\"`; the floor is the input of that last call", "",
          "## 1 · Token efficiency (median of each arm's runs)", "",
          table(tok_rows, ("metric", ncol, "B Big Dad", "Δ B vs N", "kind")), "",
          "## 2 · Management overhead", "",
          table(oh_rows, ("metric", ncol, "B Big Dad", "kind")), "",
          "Native lever for each Big Dad part:", ""] + [f"- {k}: {v}" for k, v in (d.get("native") or {}).get("tools", NATIVE)] + [
          "", "Native has no: " + "; ".join((d.get("native") or {}).get("lacks", NATIVE_LACKS)) + ".", "",
          "## 3 · Runtime", "",
          table(rt_rows, ("metric", ncol, "B Big Dad", "Δ B vs N", "kind")), ""]
    if ob.get("measured"):
        s, pl = ob["drops"].get("slim", {}), ob["drops"].get("plain", {})
        md += ["## Observational · the build chat's own receipt", "",
               f"One long chat ({ob['size_mb']} MB log, {ob['calls']} calls, {ob['compacts']} compacts of which {ob['slims']} slim). "
               "Plain compacts ran before Big Dad's slim instructions existed; slim ones after. Different content each time, so this "
               "is a pattern, not a controlled comparison.", "",
               table([("plain compact (Claude Code alone)", pl.get("n", 0), f"{pl['mean_drop_pct']:.0f}%" if pl.get("n") else "n/a",
                       f"{big(pl['mean_before'])} -> {big(pl['mean_after'])}" if pl.get("n") else "n/a"),
                      ("🥛 slim compact (Big Dad's PreCompact)", s.get("n", 0), f"{s['mean_drop_pct']:.0f}%" if s.get("n") else "n/a",
                       f"{big(s['mean_before'])} -> {big(s['mean_after'])}" if s.get("n") else "n/a")],
                     ("compact kind", "n", "mean drop per call", "mean before -> after (measured)")), "",
               f"Also counted on that chat: {ob['blocks']} double reads blocked, {ob['warns']} guard warnings, {ob['pauses']} pauses.", ""]
    md += ["## Proof · one Big Dad in B, none in N", "",
           "`bd briefs` = Big Dad's SessionStart brief per session start (one per loaded copy); `bd outputs` = every hook output "
           "carrying \"Big Dad\" in the main run; `hooks started` = every hook Claude Code launched, by event"
           + (" (N must show none); `isolation` = the run's init event and transcripts checked against stock Claude Code." if stock
              else " (other installed hooks run in both arms)."), "",
           table(proof_rows, ("run", "arm", "bd briefs", "bd outputs", "hooks started", "isolation")), "",
           f"- N: {sum(N.briefs)} briefs, {N.notes} Big Dad outputs over {N.n} runs. B: briefs per run {B.briefs}, so exactly one copy loaded.",
           f"- real `~/.claude/bigdad` untouched by the run: {rd_text}." + (" See limits." if rd_note else ""),
           f"- {iso_line}." if stock else "- isolation: not checked (run made before 1.7.0)."] + \
          ([f"- versus sessions in your real `~/.claude/projects`: {(d.get('isolation') or {}).get('real_projects_sessions', '?')}."]
           if stock else []) + ["",
           "## Per run", "",
           table(run_rows, ("run", "arm", "input", "cache read", "cache write", "output", "calls", "tokens (session)", "wall", "denied reads", "bd outputs", "floor", "status")), "",
           "## Verdict (architect)", "",
           "Big Dad wins:"] + [f"- {x}" for x in win] + ["", "Native wins:"] + [f"- {x}" for x in nat] + ["", "A wash (within 5%):"] + \
          ([f"- {x}" for x in wash] or ["- nothing within 5%"]) + \
          (["", "Not exercised (B was learning):"] + [f"- {x}" for x in skip] if skip else []) + ["", "## Limits · not measured", ""] + [f"- {x}" for x in limits] + [
          "", "Raw per-run numbers: `versus-runs.json` beside this file. Terminal print: `receipt.txt`."]
    # ---- terminal
    pad = lambda label, n, b, dl="": f"  {label:<30}{n:>10}{b:>14}{dl:>10}"      # 2+30+10+14+10 = W
    t = [f"🥛 BIG DAD · VERSUS RECEIPT 🧾",
         f"{'stock Claude Code' if stock else 'your setup w/o Big Dad'} (N) vs Big Dad {d.get('commit', '?')} (B)",
         f"B state: {STATE_TEXT[state][1]}",
         f"{model} · {N.n}+{B.n} runs · {d.get('date', '?')[:10]}",
         "═" * W,
         pad("TOKENS (measured, median/run)", "N", "B", "Δ"),
         pad("💸 total input re-sent", big(N.input), big(B.input), delta(N.input, B.input)),
         pad("🔁 re-read from cache", big(N.read), big(B.read), delta(N.read, B.read)),
         pad("📝 written to cache", big(N.written), big(B.written), delta(N.written, B.written)),
         pad("🆕 new input", big(N.new), big(B.new), delta(N.new, B.new)),
         pad("📤 output", big(N.out), big(B.out), delta(N.out, B.out)),
         pad("📈 peak call", big(N.peak), big(B.peak), delta(N.peak, B.peak)),
         pad("🧮 tokens/run (with compact)", big(N.session_tokens), big(B.session_tokens), delta(N.session_tokens, B.session_tokens)),
         pad("🧹 post-compact floor", floor(N), floor(B), delta(N.floor, B.floor) if N.floor_n and B.floor_n else ""),
         pad("🚫 double reads blocked (all)", N.denied, B.denied),
         pad("🥛 notes, est. tokens per run", est(N.note_tokens), est(B.note_tokens)),
         pad("✅ correct answers", f"{N.correct}/{N.n}", f"{B.correct}/{B.n}"),
         "─" * W,
         pad("OVERHEAD (counted / measured)", "N", "B"),
         pad("files / lines", "0 / 0", f"{oh.get('total', {}).get('files', '?')} / {oh.get('total', {}).get('lines', 0):,}"),
         pad("hook commands / events", "0 / 0", f"{oh.get('hook_commands', '?')} / {len(oh.get('hook_events', []))}"),
         pad("setup steps", "0", "1 (+2 opt)"),
         pad("hook ms per Read call", "0", f"{lat.get('per_read_call_ms', 0):.0f} ms" if lat else "n/m"),
         pad("hook ms per Bash call", "0", f"{lat.get('per_bash_call_ms', 0):.0f} ms" if lat else "n/m"),
         "─" * W,
         pad("RUNTIME (measured, median)", "N", "B", "Δ"),
         pad("⏱ wall per run", ms(N.wall), ms(B.wall), delta(N.wall, B.wall)),
         pad("duration_ms", ms(N.duration), ms(B.duration), delta(N.duration, B.duration)),
         pad("duration_api_ms", ms(N.api), ms(B.api), delta(N.api, B.api)),
         pad("🧾 receipt on the build log", "n/a", f"{ob['receipt_ms']['median'] / 1000:.1f} s" if ob.get("measured") else "n/m"),
         "─" * W,
         f"  PROOF  Big Dad briefs per run: N {'/'.join(str(x) for x in N.briefs) or '-'} · B {'/'.join(str(x) for x in B.briefs) or '-'}",
         f"         real ~/.claude/bigdad untouched: {'yes' if rd.get('untouched') else 'NO'}",
         f"         N stock, isolation problems: {len(bad_runs)} of {len(runs)} runs" if stock else
         "         N = your setup, not stock (run before 1.7.0)"]
    import textwrap
    if not rd.get("untouched"):
        t += textwrap.wrap(f"({', '.join(rd.get('changed', []))}{'; see limits' if rd_note else ''})", W - 9, initial_indent=" " * 9, subsequent_indent=" " * 9)
    t += ["═" * W, "  VERDICT"]
    for icon, lines in (("🟢", win), ("🔴", nat), ("⚪", wash), ("⏸", skip)):
        for x in lines:
            t += textwrap.wrap(f"{icon} {x}", W - 2, initial_indent="  ", subsequent_indent="     ")
    t += ["─" * W] + [l for x in limits[:2] + ([rd_note] if rd_note else []) for l in textwrap.wrap(x, W - 2, initial_indent="  ", subsequent_indent="  ")]
    t.append(f"  tokens used {used(d)} · {d.get('spec', SPEC)} · measured unless marked")
    return scrub("\n".join(md) + "\n"), scrub("\n".join(t))


def say_safe(text, stream=None):
    """Print on any console: cp1252 cannot show 🥛, so unencodable characters become '?' instead of a crash."""
    stream = stream or sys.stdout
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(enc, "replace").decode(enc, "replace") + "\n")
    stream.flush()


def cmd_receipt(args):
    out = os.path.abspath(args[0]) if args else os.getcwd()
    path = os.path.join(out, "versus-runs.json")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"versus receipt: cannot read {path}: {e}")
    md, text = build_receipt(d)
    with open(os.path.join(out, "VERSUS-RECEIPT.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(out, "receipt.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    say_safe(text)
    say_safe("  written: VERSUS-RECEIPT.md and receipt.txt beside versus-runs.json")


def main(args):
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "run":
        cmd_run(args[1:])
    elif args[0] == "receipt":
        cmd_receipt(args[1:])
    else:
        sys.exit(f"usage: bigdad.py versus run|receipt (got {args[0]})")


if __name__ == "__main__":
    main(sys.argv[1:])
