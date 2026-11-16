"""Big Dad `proof read`: the reread A/B as a command.

Two tiny headless runs in a temp folder read the same ~12.6k-char file twice. Run A has the
read guard off, run B on. For each run the measured number is the growth in input tokens
between the call that asked for the second read and the call after it: that is what the
second read added to every later call. Headless only; cleans up after itself.
Both runs set BIGDAD_LEARN=off, so the guard itself is measured even on an install still in its learning period
(where everyday sessions only count second reads); a line says so when this install is learning, or will start
learning at its next normal session start (a new install the proof ran on first).
"""
import json, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks")))
try:
    import bd_common as C  # noqa: E402  ODD_ROW, optional (bigdad.py's pattern)
except ImportError:        # seventeenth review: works without bd_common, as bigdad.py does
    C = None
ODD_ROW = getattr(C, "ODD_ROW", Exception)   # a log row that cannot be read (bd_common; missing: any error)

PROMPT = ("Use the Read tool on {f} (whole file, no offset or limit). Then call the Read tool on the "
          "exact same path again, whole file. If the second read is refused, do not retry it. "
          "Then reply with only: ok")


def sample_text(n=12_600):
    lines, i = [], 0
    while sum(len(l) + 1 for l in lines) < n:
        i += 1
        lines.append(f"line {i:04d}: the quick brown fox jumps over the lazy dog, again and again, {i * 7919 % 10007}")
    return "\n".join(lines)[:n]


def _row(line, seen, ncalls):
    """One stream-json line of deltas, read whole before anything is counted: (its call as a 0- or 1-item list, the
    keys it adds to seen, the call index of each of its Reads). Raises on a value of the wrong kind."""
    try:
        r = json.loads(line)
    except (ValueError, RecursionError):
        return [], (), []
    if not isinstance(r, dict) or r.get("type") != "assistant":   # 3.0.0: a non-object line is skipped
        return [], (), []
    msg = r.get("message") if isinstance(r.get("message"), dict) else {}   # a list/string: no message
    u, mid = msg.get("usage") or {}, msg.get("id")
    n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    new, call, rd = set(), [], []
    if (mid is None or mid not in seen) and n:   # twenty-first review: an id-less call is never a duplicate
        if mid is not None:
            new.add(mid)
        call.append(n)
    for c in msg.get("content") or []:
        if not isinstance(c, dict):
            continue
        key = ("read", mid, c.get("id"))
        if c.get("type") == "tool_use" and c.get("name") == "Read" and not (
                mid and c.get("id") and (key in seen or key in new)):
            new.add(key)                                     # the same message streamed twice: count its Read once
            rd.append(ncalls + len(call) - 1)
    return call, new, rd


def deltas(stream_lines):
    """From stream-json lines: input tokens per assistant call and the call index of each Read."""
    calls, reads = [], []
    seen = set()
    for line in stream_lines:
        try:
            call, keys, rd = _row(line, seen, len(calls))
        except ODD_ROW:
            continue                                         # 3.0.0 fifteenth review: an odd row is skipped whole
        seen.update(keys)                                    # only now is anything counted
        calls.extend(call)
        reads.extend(rd)
    if len(reads) < 2 or reads[1] + 1 >= len(calls):
        return calls, reads, None
    return calls, reads, calls[reads[1] + 1] - calls[reads[1]]


def run_once(cmd, cwd, env):
    out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                         stdin=subprocess.DEVNULL, timeout=600)
    if out.returncode != 0:
        sys.exit(f"proof run failed: {(out.stderr or out.stdout)[-400:]}")
    return deltas(out.stdout.splitlines())


def learning_note():
    """One line when this install is in its learning period (the hooks' test, bd_common.learning) or its next normal
    session start would begin one (bd_learn.would_begin), else None. Reads only."""
    try:
        if C is None:
            return None
        import bd_learn
        if C.learning():
            return (f"  This install is learning: measured here with learning set aside (BIGDAD_LEARN=off); in "
                    f"everyday sessions the guard only counts second reads until the baseline covers "
                    f"{bd_learn.min_calls()} calls.")
        if bd_learn.would_begin():
            return (f"  Learning has not started on this install yet: measured here with learning set aside "
                    f"(BIGDAD_LEARN=off); everyday sessions will first only count second reads until the baseline "
                    f"covers {bd_learn.min_calls()} calls.")
    except Exception:
        pass
    return None


def cmd_proof_read(args):
    exe = shutil.which("claude")
    if not exe:
        print("🥛 == PROOF read: claude CLI not on PATH")
        return
    model = args[args.index("--model") + 1] if "--model" in args and args.index("--model") + 1 < len(args) else "haiku"
    d = tempfile.mkdtemp(prefix="bigdad-proof-")
    fp = os.path.join(d, "sample.txt")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(sample_text())
    cmd = [exe, "-p", PROMPT.format(f=fp.replace("\\", "/")), "--model", model, "--strict-mcp-config",
           "--output-format", "stream-json", "--verbose", "--no-session-persistence", "--allowedTools", "Read"]
    note = learning_note()            # this install's state, as found before the runs
    base = dict(os.environ, BIGDAD_GUARD="off", BIGDAD_WATCH="off", BIGDAD_LEARN="off",   # the guard itself;
                BIGDAD_PROBE="1")                 # Big Dad's own run: it takes no learned line
    try:
        a = run_once(cmd, d, dict(base, BIGDAD_READ_GUARD="off"))
        b = run_once(cmd, d, dict(base, BIGDAD_READ_GUARD="on"))
    finally:
        shutil.rmtree(d, True)
    print(f"🥛 == PROOF read [claude, headless, {model}] same {len(sample_text()):,}-char file read twice (measured)")
    for name, (calls, reads, delta) in (("guard off", a), ("guard on ", b)):
        if delta is None:
            print(f"  {name}: could not measure (calls {len(calls)}, reads {len(reads)}); the plugin's read guard may not be installed")
        else:
            print(f"  {name}: 2nd read added {delta:,} tokens to every later call  (calls {len(calls)}, reads {len(reads)})")
    if a[2] and b[2] is not None:
        print(f"  guard saves {a[2] - b[2]:,} tokens per call after a repeated read ({(a[2] - b[2]) / a[2]:.0%}).")
    if note:
        print(note)
    print("  Headless only: interactive sessions and agents run the same hook, so the saving carries over, but is not measured here.")
