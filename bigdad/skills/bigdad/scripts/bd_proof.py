"""Big Dad `proof read`: the reread A/B as a command.

Two tiny headless runs in a temp folder read the same ~12.6k-char file twice. Run A has the
read guard off, run B on. For each run the measured number is the growth in input tokens
between the call that asked for the second read and the call after it: that is what the
second read added to every later call. Headless only; cleans up after itself.
"""
import json, os, shutil, subprocess, sys, tempfile

PROMPT = ("Use the Read tool on {f} (whole file, no offset or limit). Then call the Read tool on the "
          "exact same path again, whole file. If the second read is refused, do not retry it. "
          "Then reply with only: ok")


def sample_text(n=12_600):
    lines, i = [], 0
    while sum(len(l) + 1 for l in lines) < n:
        i += 1
        lines.append(f"line {i:04d}: the quick brown fox jumps over the lazy dog, again and again, {i * 7919 % 10007}")
    return "\n".join(lines)[:n]


def deltas(stream_lines):
    """From stream-json lines: input tokens per assistant call and the call index of each Read."""
    calls, reads = [], []
    seen = set()
    for line in stream_lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "assistant":
            continue
        msg = r.get("message") or {}
        u, mid = msg.get("usage") or {}, msg.get("id")
        n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if mid not in seen and n:
            seen.add(mid)
            calls.append(n)
        for c in msg.get("content") or []:
            key = ("read", mid, c.get("id")) if isinstance(c, dict) else None
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Read" and not (mid and c.get("id") and key in seen):
                seen.add(key)                                # the same message streamed twice: count its Read once
                reads.append(len(calls) - 1)
    if len(reads) < 2 or reads[1] + 1 >= len(calls):
        return calls, reads, None
    return calls, reads, calls[reads[1] + 1] - calls[reads[1]]


def run_once(cmd, cwd, env):
    out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                         stdin=subprocess.DEVNULL, timeout=600)
    if out.returncode != 0:
        sys.exit(f"proof run failed: {(out.stderr or out.stdout)[-400:]}")
    return deltas(out.stdout.splitlines())


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
    base = dict(os.environ, BIGDAD_GUARD="off", BIGDAD_WATCH="off")
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
    print("  Headless only: interactive sessions and agents run the same hook, so the saving carries over, but is not measured here.")
