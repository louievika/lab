#!/usr/bin/env python3
"""Big Dad: find what Claude Code re-reads, and what it loads on every call.

Read-only, standard library only. Reads Claude Code's own session logs (~/.claude, or
$CLAUDE_CONFIG_DIR), which record each request's shape and the token usage the API returned.

  python bigdad.py all [N]          everything below, for the last N sessions (default 10)
  python bigdad.py reread [N]       re-sent context: totals, heaviest sessions, costliest results
  python bigdad.py modes            overhead by execution mode: interactive, headless, subagent, workflow
  python bigdad.py tools [N]        per mode, the newest N logs: measured first call + the tools it carried
  python bigdad.py load             startup load of the newest session, by source
  python bigdad.py usage [N]        tokens per session
  python bigdad.py skills           listed skills vs. ever used; MCP calls per server
  python bigdad.py files            instruction files (CLAUDE.md, CLAUDE.local.md)
  python bigdad.py compare A B      first-call tokens of session A vs. B (id prefixes)
  python bigdad.py watch            watch the rest of this session (with the plugin's watch hook)
  python bigdad.py note "TEXT"      add a one-line note: what wasted tokens, what Big Dad should change
  python bigdad.py notes [N]        read back watch notes: big outputs by tool + agent notes
  python bigdad.py probe FLAGS...   A/B test a cut with two tiny headless runs (headless only:
                                    interactive sessions and agents load tools differently; use `tools`)
  python bigdad.py models [HOURS]   tokens by model x mode (interactive, headless, subagent type, fork,
                                    workflow); flags forks, agents with no explicit model, parallel big agents
  python bigdad.py health           security + upkeep checks on the setup (proposes, never changes)
  python bigdad.py fresh            setup snapshot + diff, changelog scan, retire-check vs native levers
  python bigdad.py proof read       the reread A/B: read guard off vs on, two tiny headless runs
  python bigdad.py setup            tune Big Dad's own knobs to this machine -> profile.json (auto when stale)
  python bigdad.py agents [HOURS]   subagents seen by the monitor hook: running + finished, model, size, flags
  python bigdad.py report           one self-contained local HTML page with the charts; prints its path
  python bigdad.py receipt [session]  this chat's receipt: tokens sent, each compact before -> after
                                     (in Claude Code, type receipt: shown at once, zero tokens)
  python bigdad.py versus run [--runs 3] [--out DIR] [--transcript LOG] [--commit REV] [--cap TOKENS] [--root DIR]
                                    [--learning]  B left in its learning period (default: B acts)
                                    A/B: stock Claude Code vs the same + Big Dad (guild/versus-spec.md); costs real tokens
  python bigdad.py versus receipt [DIR]   VERSUS-RECEIPT.md + terminal receipt from DIR/versus-runs.json, offline
  python bigdad.py settle           each area settled at its floor, moving, or grown back (`settle reset`)
  python bigdad.py self [DAYS] [--json]  Big Dad's own cost vs what it saved, per part (default 14 days);
                                    names the switch for a part that loses, never flips it
  python bigdad.py baseline [--save] the starting point: newest 20 chats, re-sent share, startup, above the guide
  python bigdad.py refresh [--cwd DIR] [--quiet]  the startup upkeep (SessionStart runs it in the background):
                                    profile only when the setup moved, baseline when stale
  python bigdad.py tidy [yes|undo|empty]  Big Dad's own leftovers (state files, its test runs); dry run first
  python bigdad.py outline [auto|on|off]  big whole-file Reads get an outline first (auto = helpers only)
  python bigdad.py keep [x ["why"]|--list|--remove x|verbose]  what you chose on purpose: shows its cost, never
                                    a cut (`keep verbose` = no trimming; a "no" to a proposal keeps it too)
  python bigdad.py shrink [on|off]  big noisy Bash output cut before it enters the chat (on by default);
                                    no argument: status + tokens cut so far, by command type
  python bigdad.py pause N|off      main-chat pause at N x the guide (off by default; interactive only)
  python bigdad.py helper-floor auto|off|4k  helpers: a re-read under the floor goes through (default auto:
                                    the helper's call size / its calls so far; off = deny every re-read)
  python bigdad.py agent-stop 100k|N|off  opt-in hard stop for subagents: 100k = tokens per call, 2 = 2x the
                                    guide (default off); capped agents write a handoff, the main gets a relaunch line
  python bigdad.py sentinel on|off|check  opt-in: one user-level SessionStart hook outside the plugin folder that
                                    says so when a plugin can't load (folder moved, hooks stopped running)
  python bigdad.py statusline on|off [--replace]  opt-in: Big Dad's status line (keeps yours unless --replace)

Options (anywhere):
  --redact               for sharing: hides home path, user name, email, command text,
                         session ids, and any words given with --private
  --private a,b,c        extra words to hide (also read from $BIGDAD_PRIVATE)

Numbers marked "≈" are estimates (text ≈ 4 characters per token, an image ≈ 1,600
tokens). "share" rows split a measured total by size. Everything else is read
straight from the API usage the agent logged.
"""
import glob, json, os, re, shutil, statistics, subprocess, sys, tempfile, time
from collections import Counter, defaultdict

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
HOOKS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "hooks"))
sys.path.insert(0, HOOKS_DIR)
try:
    import bd_common as BC              # one estimator for hooks and reports: images as images, never base64
except ImportError:
    BC = None
IMG_TOKENS = getattr(BC, "IMG_TOKENS", 1600)
REDACT = False
ALIASES = {}
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ------------------------------------------------------------------ helpers
def _open(path, mode="r", **kw):
    """open(); in a command a file held open elsewhere (Windows) is waited out first (bd_common.open_wait)."""
    return (BC.open_wait if BC else open)(path, mode, **kw)


def rows(path, needle=None):
    try:
        f = _open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            if needle and needle not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def k(n):
    return f"{n/1000:,.1f}k" if n >= 1000 else str(n)


def big(n):
    if n >= 1e9:
        return f"{n/1e9:,.2f}B"
    if n >= 1e6:
        return f"{n/1e6:,.1f}M"
    return k(n)


def newest(pattern):
    return sorted(glob.glob(pattern, recursive=True), key=os.path.getmtime, reverse=True)


def started(path):
    """When a log began: its first timestamp (a chat that's still running keeps changing its file time)."""
    try:
        with _open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                m = re.search(r'"timestamp":\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
                if i > 40:
                    break
    except OSError:
        pass
    return ""


def alias(raw):
    """Session id as shown. With --redact, a stable short label instead."""
    if not REDACT:
        return raw
    return ALIASES.setdefault(raw, f"S{len(ALIASES) + 1:02d}")


def shown_arg(name, key):
    """What a tool call touched. Under --redact: only the kind of thing, never its name."""
    if not key:
        return ""
    if REDACT:
        if name in ("Bash", "PowerShell", "exec", "shell"):
            return "(command)"
        if name in ("Read", "Write", "Edit"):
            ext = os.path.splitext(key.rstrip("/\\"))[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                return f"a {ext} image"
            return f"a {ext} file" if ext else "a file"
        return "(hidden)"
    if name in ("Read", "Write", "Edit", "Glob", "Grep"):
        key = os.path.basename(key.rstrip("/\\")) or key
    return key[:48]


def who(label):
    """Session label in result lists. Under --redact: just main or subagent."""
    if not REDACT:
        return label
    return "subagent" if label.endswith("*") else "main"


def est_tokens(body):
    """(tokens, kind) for a tool result: text ≈ chars/4; an image ≈ width*height/750 (≤ IMG_TOKENS), never its base64."""
    if BC is not None:
        return BC.est_tokens(body)
    if isinstance(body, list):
        imgs = sum(1 for b in body if isinstance(b, dict) and b.get("type") == "image")
        text = sum(len(b.get("text") or "") for b in body if isinstance(b, dict) and b.get("type") == "text")
        return imgs * IMG_TOKENS + text // 4, "image" if imgs else "text"
    return len(body if isinstance(body, str) else json.dumps(body)) // 4, "text"


def print_load(label, total, parts, detail, top=5):
    chars = sum(parts.values()) or 1
    print(f"🥛 == LOAD before the first message  [{label}]")
    print(f"  {total:,} input tokens on call 1 (measured). Rows split that by size:")
    for name, n in parts.most_common():
        print(f"  {total*n/chars:>8,.0f}  {name}")
        for sub, m in detail[name].most_common(top):
            if name.startswith("CLAUDE.md"):
                sub = os.path.basename(sub)
            print(f"  {'':>8}    {total*m/chars:>7,.0f}  {sub}")


# ------------------------------------------------------------------ Claude Code
class Claude:
    name = "claude"

    def __init__(self):
        self.proj = os.path.join(CLAUDE_DIR, "projects")

    def present(self):
        return bool(glob.glob(os.path.join(self.proj, "*", "*.jsonl")))

    def sessions(self):
        return newest(os.path.join(self.proj, "*", "*.jsonl"))

    def raw_id(self, p):
        return os.path.basename(p)[:8]

    def sid(self, p):
        return alias(self.raw_id(p))

    @staticmethod
    def subagent_logs(p):
        return glob.glob(os.path.join(p[:-6], "subagents", "*.jsonl"))

    @staticmethod
    def walk(path):
        """One ordered pass. Returns per-call usage and every tool result with its call index."""
        seen, calls, comp, names, results, got = set(), [], [], {}, [], set()
        date = ""
        for r in rows(path):
            t = r.get("type")
            date = date or (r.get("timestamp") or "")[:10]
            if t == "system" and "compact" in str(r.get("subtype", "")):
                comp.append(len(calls))
                continue
            m = r.get("message") or {}
            if t == "assistant":
                u, mid = m.get("usage") or {}, m.get("id")
                if u and mid not in seen:
                    seen.add(mid)
                    new = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                    old = u.get("cache_read_input_tokens") or 0
                    if new + old:
                        calls.append((new, old, u.get("output_tokens") or 0))
                for c in m.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        key = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("url") or ""
                        names[c.get("id")] = (c.get("name") or "?", str(key), (inp.get("offset"), inp.get("limit")))
            elif t == "user" and isinstance(m.get("content"), list):
                for c in m["content"]:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        tid = c.get("tool_use_id")
                        if tid and tid in got:               # the same result logged twice: count it once
                            continue
                        got.add(tid)
                        tok, kind = est_tokens(c.get("content"))
                        nm, key, rng = names.get(c.get("tool_use_id"), ("?", "", None))
                        results.append((len(calls), tok, kind, nm, key, rng))
        return {"calls": calls, "comp": comp, "results": results, "date": date}

    def first_call(self, path):
        shape, atts = None, []
        for r in rows(path):
            t = r.get("type")
            if t == "api-request-shape" and shape is None:
                shape = r.get("shape")
            elif t == "attachment":
                atts.append(r.get("attachment") or {})
            elif t == "assistant":
                u = (r.get("message") or {}).get("usage") or {}
                total = sum(u.get(f) or 0 for f in ("input_tokens",
                            "cache_creation_input_tokens", "cache_read_input_tokens"))
                if total:
                    return shape, atts, total
        return shape, atts, None

    def load_parts(self, path):
        shape, atts, total = self.first_call(path)
        if not total:
            return None
        parts, detail = Counter(), defaultdict(Counter)
        if shape:
            parts["system prompt"] = len(json.dumps(shape.get("system", [])))
            for tool in shape.get("tools", []):
                n = len(json.dumps(tool))
                parts["tool schemas"] += n
                detail["tool schemas"][tool.get("name", "?")] = n
        for a in atts:
            ty = a.get("type", "?")
            if ty == "deferred_tools_delta":
                for line in a.get("addedLines") or []:
                    m = re.match(r"mcp__(.+?)__", line)
                    grp = "MCP " + m.group(1) if m else "built-in"
                    parts["deferred tool names"] += len(line) + 1
                    detail["deferred tool names"][grp] += len(line) + 1
            elif ty == "skill_listing":
                body = a.get("content") or ""
                parts["skill list"] += len(body)
                for line in body.split("\n- "):
                    detail["skill list"][line.lstrip("- ").split(": ", 1)[0]] += len(line)
            elif ty == "instructions":
                for f in a.get("files") or []:
                    n = len(f.get("content") or "")
                    parts["CLAUDE.md + memory"] += n
                    detail["CLAUDE.md + memory"][f.get("path", "?")] = n
            elif ty == "hook_success":
                n = len(a.get("content") or "")
                parts["hook output"] += n
                detail["hook output"][a.get("hookName", "?")] += n
            elif ty in ("prompt_snapshot", "remote_session_change"):
                continue  # log-only copies, not sent to the model
            elif ty in ("agent_listing_delta", "mcp_instructions_delta"):
                parts[ty.replace("_delta", "").replace("_", " ")] += len(json.dumps(a))
            else:
                parts["other"] += len(json.dumps(a))
                detail["other"][ty] += len(json.dumps(a))
        return total, parts, detail

    def load(self, path=None):
        for p in ([path] if path else self.sessions()):
            res = self.load_parts(p)
            if res:
                print_load(f"claude {self.sid(p)}", *res)
                return
        print("🥛 == LOAD [claude] no session with a recorded first call")

    def usage(self, n):
        print(f"🥛 == USAGE [claude] last {n} sessions")
        print("  session   date        calls      sent   output  subagents   peak/call   avg/call")
        for p in self.sessions()[:n]:
            w = self.walk(p)
            calls = w["calls"]
            sent = sum(a + b for a, b, _ in calls)
            sub = sum(sum(a + b for a, b, _ in self.walk(sp)["calls"]) for sp in self.subagent_logs(p))
            peak = max((a + b for a, b, _ in calls), default=0)
            print(f"  {self.sid(p):<8}  {w['date']:10}  {len(calls):>5}  {big(sent):>8}  {k(sum(c[2] for c in calls)):>7}"
                  f"  {big(sub):>9}  {k(peak):>10}  {k(sent // max(len(calls), 1)):>9}")

    def reread(self, n):
        files = self.sessions()[:n] if n else self.sessions()
        new = old = boot = 0
        per, carried, repeats = [], [], []
        for p in files:
            logs = [(p, self.sid(p))] + [(sp, self.sid(p) + "*") for sp in self.subagent_logs(p)]
            s_new = s_old = s_sub = 0
            info = None
            for lp, label in logs:
                w = self.walk(lp)
                calls, comp = w["calls"], w["comp"]
                if not calls:
                    continue
                cn = sum(c[0] for c in calls)
                co = sum(c[1] for c in calls)
                first = calls[0][0] + calls[0][1]
                boot += first * len(calls)
                if lp == p:
                    info = (len(calls), len(comp), max(a + b for a, b, _ in calls), w["date"])
                else:
                    s_sub += cn + co
                s_new, s_old = s_new + cn, s_old + co
                end = len(calls)
                last_read = {}
                for at, tok, kind, nm, key, rng in w["results"]:
                    nxt = next((c for c in comp if c > at), end)
                    carried.append((tok * max(nxt - at, 0), tok, max(nxt - at, 0), label, nm, kind, key))
                    if nm == "Read" and key:
                        prev = last_read.get((key, rng))
                        if prev is not None and not any(prev < c <= at for c in comp):
                            repeats.append((tok * max(nxt - at, 0), tok, label, key))
                        last_read[(key, rng)] = at
            new, old = new + s_new, old + s_old
            if info:
                per.append((s_new + s_old, self.sid(p), info, s_old, s_sub))
        sent = new + old
        print(f"🥛 == RE-READS [claude] {len(files)} sessions + their subagents (measured API usage)")
        if not sent:
            print("  no usage recorded")
            return
        print(f"  {big(sent)} tokens sent. Only {big(new)} ({new/sent:.1%}) were new.")
        print(f"  {big(old)} ({old/sent:.1%}) were old context re-sent: every new token went out"
              f" {old/max(new,1):,.0f} more times.")
        print(f"  The startup load, sent again on every call, was {big(boot)} of that ({boot/sent:.0%});"
              f" config cuts shrink this part.")
        print("  heaviest sessions:")
        print("    session    date        calls  compacts      sent   re-sent   peak/call   subagents")
        for i, (s, sid_, (nc, ncomp, peak, date), so, ss) in enumerate(sorted(per, reverse=True)[:6], 1):
            print(f"    {sid_:<9}  {date:10}  {nc:>5}  {ncomp:>8}  {big(s):>8}  {so/max(s,1):>7.0%}"
                  f"  {k(peak):>10}  {big(ss):>10}")
        total_carry = sum(c[0] for c in carried)
        print(f"  tool results kept in context and re-sent on later calls: ≈{big(total_carry)} tokens.")
        print("  heaviest single results (≈ size x later calls" + (")" if REDACT else "; * = subagent):"))
        for cost, tok, ncalls, label, nm, kind, key in sorted(carried, reverse=True)[:8]:
            label = who(label)
            what = nm + (" image" if kind == "image" else "")
            arg = shown_arg(nm, key)
            print(f"    ≈{big(cost):>7}  {k(tok):>7} x {ncalls:>4} calls  {label:<9}  {what:<12} {arg}")
        rep = sum(r[0] for r in repeats)
        print(f"  same file + range read again while the first copy was still in context:"
              f" {len(repeats)} times, ≈{big(rep)} tokens.")
        for cost, tok, label, key in sorted(repeats, reverse=True)[:3]:
            print(f"    ≈{big(cost):>7}  {who(label):<9}  {shown_arg('Read', key)}")

    def skills(self):
        listed, src = {}, ""
        logs = sorted(glob.glob(os.path.join(self.proj, "**", "*.jsonl"), recursive=True), key=started, reverse=True)
        for p in logs[:300]:
            src = "newest-started log with a skill list, " + started(p)[:16].replace("T", " ") + " UTC"
            for r in rows(p, "skill_listing"):
                a = r.get("attachment") or {}
                if a.get("type") == "skill_listing" and a.get("isInitial"):
                    for line in (a.get("content") or "").split("\n- "):
                        name, _, desc = line.lstrip("- ").partition(": ")
                        listed[name] = len(line)
                        SKILL_DESC[name] = desc[:200]
            if listed:
                break
        used, mcp = Counter(), Counter()
        files = glob.glob(os.path.join(self.proj, "**", "*.jsonl"), recursive=True)
        # per-file usage counts, keyed by size+mtime: a closed log never changes, so only new/grown logs are read again
        cache_path = os.path.join(CLAUDE_DIR, "bigdad", "usage-cache.json")
        try:
            with _open(cache_path, encoding="utf-8", errors="replace") as f:
                cache = json.load(f)
            cache = cache if isinstance(cache, dict) else {}
        except (OSError, ValueError):
            cache = {}
        fresh, dirty = {}, False
        for p in files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            key, stamp = p.replace("\\", "/"), [st.st_size, int(st.st_mtime)]
            hit = cache.get(key)
            if hit and hit[:2] == stamp:
                fresh[key] = hit
            else:
                u, m = scan_usage(p)
                fresh[key] = stamp + [u, m]
                dirty = True
            used.update(fresh[key][2]); mcp.update(fresh[key][3])
        if dirty or len(fresh) != len(cache):
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with _open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(fresh, f)
            except OSError:
                pass
        try:
            oldest = min(os.path.getmtime(p) for p in files) if files else time.time()
        except (OSError, ValueError):
            oldest = time.time()
        report_skills("claude", listed, used, len(files), src, days=(time.time() - oldest) / 86400)
        keep = __import__("bd_keep")
        kept_all = keep.items()
        print("  MCP tool calls per server, all logs (check before cutting a server):",
              ", ".join(f"{n} {c}" + (" (kept, your choice)" if keep.match(n, "mcp", kept_all) else "")
                        for n, c in mcp.most_common()) or "none")
        print("  cut levers: claude.ai connectors toggle at claude.ai; local servers via `disabledMcpServers` or /mcp;"
              " ENABLE_TOOL_SEARCH keeps only names in context (default on).")
        return listed, used

    def probe_cmd(self, extra):
        fixed = [] if "--strict-mcp-config" in extra else ["--strict-mcp-config"]
        note = "MCP held off on both sides (MCP servers connect at random times)" if fixed else ""
        base = ["claude", "-p", "Reply with only: ok", "--output-format", "json", "--no-session-persistence"]
        return base + fixed, base + fixed + extra, note

    @staticmethod
    def probe_tokens(stdout):
        u = json.loads(stdout)["usage"]
        return u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]


# ------------------------------------------------------------------ shared
def scan_usage(path):
    """(skill name -> uses, MCP server -> calls) in one log. Cheap substring checks first: most lines have none."""
    used, mcp = Counter(), Counter()
    with _open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"Skill"' in line:
                for m in re.finditer(r'"name":\s*"Skill",\s*"input":\s*\{"skill":\s*"([^"]+)"', line):
                    used[m.group(1)] += 1
            if "<command-name>" in line:
                for m in re.finditer(r"<command-name>/?([\w:.-]+)</command-name>", line):
                    used[m.group(1)] += 1
            if "mcp__" in line:
                for m in re.finditer(r'"name":\s*"mcp__(.+?)__[^"]*",\s*"input"', line):
                    mcp[m.group(1)] += 1
    return dict(used), dict(mcp)


SKILL_DESC = {}
HISTORY_DAYS = 30           # never suggest cutting a skill on less history than this
OVERLAP = re.compile(r"token usage|cache-read|less tokens|context (window|health|size)|\bcompact|terse|concise|"
                     r"brevity|caveman|handoff", re.I)


def report_skills(agent, listed, used, nfiles, src="", days=None):
    if not listed:
        print(f"🥛 == SKILLS [{agent}] no skill list found in the logs")
        return
    keep = __import__("bd_keep")
    kept_all = keep.items()
    kept = [n for n in listed if keep.match(n, "skill", kept_all) or keep.match(n, "plugin", kept_all)]
    never = sorted((n for n in listed if not used[n] and n not in kept), key=lambda n: -listed[n])
    span = f" over {days:.0f} days" if days is not None else ""
    print(f"🥛 == SKILLS [{agent}] {len(listed)} listed ({src}); usage from {nfiles} logs{span}")
    print("  Big Dad never deletes a skill: every option below is reversible and waits for your yes.")
    own = [n for n in listed if n != "bigdad" and not n.startswith("bigdad:") and OVERLAP.search(n + " " + SKILL_DESC.get(n, ""))]
    if own:
        print("  your skills that already cover part of this job (Big Dad points to them instead of duplicating them):")
        print("    " + ", ".join(f"{n} ({used[n]} uses)" for n in sorted(own, key=lambda n: -used[n])[:8]))
    print("  (config cuts show up only in sessions started after the cut)")
    hits = [f"{n} {used[n]}" for n in sorted(listed, key=lambda n: -used[n]) if used[n]]
    print("  used:", ", ".join(hits) or "none")
    ch = sum(listed[n] for n in never)
    print(f"  never used: {len(never)} (≈{ch // 4:,} tokens of listing on every call)")
    if kept:
        print(f"  kept by you: {len(kept)} (≈{sum(listed[n] for n in kept) // 4:,} tokens of listing per call, your choice;"
              " never a cut): " + ", ".join(sorted(kept, key=lambda n: -listed[n])))
    if days is not None and days < HISTORY_DAYS:
        if never:
            top = never[:8]
            print(f"    biggest never used: " + ", ".join(f"{n} (≈{listed[n] // 4:,})" for n in top)
                  + (f", +{len(never) - len(top)} more" if len(never) > len(top) else "") + ". Names only, not a cut.")
        print(f"    only {days:.0f} days of logs (under {HISTORY_DAYS}): no skill cut suggested yet. A skill unused for a few days")
        print("    may still be one you rely on. Run `skills` again later.")
        return
    home = os.path.join(CLAUDE_DIR, "skills")
    mine = [n for n in never if ":" not in n and os.path.lexists(os.path.join(home, n))]
    plug = [n for n in never if ":" in n]
    rest = [n for n in never if n not in mine and n not in plug]
    if mine:
        print(f"    yours ({len(mine)}, ≈{sum(listed[n] for n in mine) // 4:,} tokens):", ", ".join(mine))
        print("      native lever first: `disable-model-invocation: true` in SKILL.md keeps /name but drops it from the list;")
        print("      `skillOverrides` (settings) can set user-invocable-only or off per skill. Last resort: move to ~/.claude/skills-off.")
    if plug:
        print(f"    from plugins ({len(plug)}; skillOverrides doesn't reach these): turn the plugin off after checking its used skills:", ", ".join(plug))
    if rest:
        print(f"    built in, can't be cut here ({len(rest)}):", ", ".join(rest))


def cmd_files():
    print("🥛 == INSTRUCTION FILES (an agent loads these when it starts in or below their folder)")
    names = {"CLAUDE.md", "CLAUDE.local.md"}
    skip = {"node_modules", ".git", "AppData", "Library", "target", "dist", "build",
            "site-packages", "venv", ".venv", "plugins", "registry", "vendor_imports"}
    hits = []
    for root, dirs, fs in os.walk(HOME):
        depth = root[len(HOME):].count(os.sep)
        dirs[:] = [d for d in dirs if d not in skip and depth < 5
                   and (not d.startswith(".") or d == ".claude")]
        hits += [(os.path.getsize(os.path.join(root, f)), os.path.join(root, f)) for f in fs if f in names]
    if not hits:
        print("  none found")
    for size, fp in sorted(hits, reverse=True)[:20]:
        if REDACT:   # size, name and depth only: folder names can name projects and people
            print(f"  {size:>7,}  {os.path.basename(fp)}  (depth {fp[len(HOME):].count(os.sep)} under ~)")
        else:
            print(f"  {size:>7,}  {fp}")
    dup = defaultdict(list)
    for _, fp in hits:
        try:
            dup[open(fp, encoding="utf-8", errors="replace").read()].append(fp)
        except OSError:
            pass
    for text, fps in dup.items():
        if len(fps) > 1 and text.strip():
            print(f"  identical text in {len(fps)} files" + ("" if REDACT else ": " + " | ".join(fps)))
    mem = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "memory", "*.md"))
    if mem:
        print(f"  claude memory files: {len(mem)} ({sum(map(os.path.getsize, mem)):,} bytes;"
              " only each MEMORY.md index loads at start)")


def cmd_probe(agent, extra):
    exe = "claude"
    if not shutil.which(exe):
        print(f"🥛 == PROBE [{agent.name}] {exe} CLI not on PATH")
        return
    a_cmd, b_cmd, note = agent.probe_cmd(extra)
    d = tempfile.mkdtemp(prefix="bigdad-probe-")

    def once(cmd):
        cmd = [shutil.which(cmd[0])] + cmd[1:]
        out = subprocess.run(cmd, cwd=d, capture_output=True, text=True, encoding="utf-8",
                             stdin=subprocess.DEVNULL, timeout=300)
        try:
            return agent.probe_tokens(out.stdout)
        except Exception:
            sys.exit(f"probe failed: {(out.stderr or out.stdout)[-400:]}")

    try:
        base, cut = once(a_cmd), once(b_cmd)
    finally:
        shutil.rmtree(d, True)
    print(f"🥛 == PROBE [{agent.name}] startup input tokens, two runs back to back (measured)")
    if note:
        print(f"  {note}")
    print(f"  as-is {base:,} -> with [{' '.join(extra) or 'nothing'}] {cut:,}"
          f" = {cut-base:+,} ({(cut-base)/max(base,1):+.0%})")


class Redact:
    """Stdout filter for --redact: home path -> ~, user name/email/private words -> <private>."""

    def __init__(self, out, private):
        import getpass
        self.out = out
        words = {os.path.basename(HOME)} | set(private)
        try:
            words.add(getpass.getuser())
        except Exception:
            pass
        homes = {HOME, HOME.replace("\\", "/")}
        for h in list(homes):
            if re.match(r"^[A-Za-z]:", h):  # C:/Users/x -> /c/Users/x (Git Bash form)
                homes.add("/" + h[0].lower() + h[2:].replace("\\", "/"))
        self.home = [re.compile(re.escape(h), re.I) for h in sorted(homes, key=len, reverse=True)]
        self.words = [re.compile(re.escape(w), re.I) for w in sorted(words, key=len, reverse=True) if len(w) > 2]
        self.words.append(re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"))  # email addresses
        self.words.append(re.compile(r"C--Users-[^\\/\s]+"))       # encoded project folder names

    def write(self, s):
        for p in self.home:
            s = p.sub("~", s)
        for p in self.words:
            s = p.sub("<private>", s)
        return self.out.write(s)

    def flush(self):
        self.out.flush()


def mode_of(path, entry):
    """Execution mode of one Claude Code log, from where it lives and how it was started."""
    s = path.replace("\\", "/")
    if "/workflows/" in s:
        return "workflow agent"
    if "/subagents/" in s:
        return "subagent"
    return "headless (-p / SDK)" if entry and entry != "cli" else "interactive"


def cmd_modes(agent):
    """Per execution mode: share of tokens, startup load, repeated context, and what the
    logged startup request contained. Everything is read from the logs (verified)."""
    files = glob.glob(os.path.join(agent.proj, "**", "*.jsonl"), recursive=True)
    keys = ("logs", "calls", "sent", "cached", "instr")
    S = defaultdict(lambda: {**{k: 0 for k in keys}, "first": [], "tools": [], "system": [], "skills": []})
    for p in files:
        entry = shape = first = None
        skill, instr, seen, calls, sent, cached = 0, False, set(), 0, 0, 0
        with _open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if entry is None and '"entrypoint"' in line:
                    m = re.search(r'"entrypoint":\s*"([^"]+)"', line)
                    entry = m.group(1) if m else None
                if not any(n in line for n in ('"usage"', "api-request-shape", '"attachment"')):
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = r.get("type")
                if t == "api-request-shape" and shape is None:
                    shape = r.get("shape") or {}
                elif t == "attachment" and first is None:
                    a = r.get("attachment") or {}
                    if a.get("type") == "skill_listing":
                        skill += len(a.get("content") or "")
                    elif a.get("type") == "instructions":
                        instr = True
                elif t == "assistant":
                    msg = r.get("message") or {}
                    u, mid = msg.get("usage") or {}, msg.get("id")
                    if not u or mid in seen:
                        continue
                    seen.add(mid)
                    n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    if n:
                        calls, sent, cached = calls + 1, sent + n, cached + (u.get("cache_read_input_tokens") or 0)
                        first = first or n
        if not calls:
            continue
        s = S[mode_of(p, entry)]
        s["logs"] += 1; s["calls"] += calls; s["sent"] += sent; s["cached"] += cached; s["instr"] += instr
        s["first"].append(first); s["skills"].append(skill)
        if shape is not None:
            s["tools"].append(len(shape.get("tools", []))); s["system"].append(len(json.dumps(shape.get("system", []))))
    total = sum(s["sent"] for s in S.values())
    print(f"🥛 == MODES [claude] {sum(s['logs'] for s in S.values())} logs, {big(total)} input tokens (verified: API usage + logged request shapes)")
    if not total:
        print("  no usage recorded")
        return
    med = lambda v: int(statistics.median(v)) if v else 0
    print("  mode                  logs   share   calls/log   startup   tokens/call   cached   tools   system chars   skill list   CLAUDE.md")
    for name, s in sorted(S.items(), key=lambda kv: -kv[1]["sent"]):
        print(f"  {name:<20} {s['logs']:>5}  {s['sent'] / total:>6.1%}   {s['calls'] / s['logs']:>9.0f}   {k(med(s['first'])):>7}"
              f"   {k(s['sent'] // s['calls']):>11}   {s['cached'] / max(s['sent'], 1):>6.1%}   {med(s['tools']):>5}"
              f"   {med(s['system']):>12,}   {med(s['skills']):>10,}   {s['instr']}/{s['logs']}")
    cached_all = sum(s["cached"] for s in S.values())
    print(f"  overall: {cached_all / total:.1%} of all input was context re-read from earlier calls; {1 - cached_all / total:.1%} was new.")
    print("  startup = input tokens on a log's first call (median). cached = share of input served from the prompt cache,")
    print("  i.e. context re-sent from earlier calls. Remote/cloud agents keep no local logs and are not measured here.")


def cmd_tools(agent, n=2):
    """Per execution mode, the newest N logs: the first call's measured input tokens and the
    tools that request carried (sizes ≈ JSON chars / 4). Run it before and after a cut: the
    newer rows show the cut in that mode. This is how to prove a cut for interactive sessions
    and agents, which a headless probe can't (they load tools differently)."""
    files = sorted(glob.glob(os.path.join(agent.proj, "**", "*.jsonl"), recursive=True), key=started, reverse=True)
    shown = Counter()
    print(f"🥛 == TOOLS [claude] newest-started {n} logs per mode (UTC start): first call (verified) and the tools it carried (≈ size)")
    for p in files:
        if all(shown[m] >= n for m in ("interactive", "subagent", "workflow agent", "headless (-p / SDK)")):
            break
        entry = shape = first = None
        with _open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if entry is None and '"entrypoint"' in line:
                    m = re.search(r'"entrypoint":\s*"([^"]+)"', line)
                    entry = m.group(1) if m else None
                if shape is None and "api-request-shape" in line:
                    try:
                        shape = json.loads(line).get("shape") or {}
                    except ValueError:
                        pass
                elif first is None and '"usage"' in line:
                    try:
                        u = (json.loads(line).get("message") or {}).get("usage") or {}
                    except ValueError:
                        continue
                    first = sum(u.get(x) or 0 for x in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) or None
                if shape is not None and first:
                    break
        mode = mode_of(p, entry)
        if shape is None or not first or shown[mode] >= n:
            continue
        shown[mode] += 1
        tools = sorted(((len(json.dumps(t)) // 4, t.get("name", "?")) for t in shape.get("tools", [])), reverse=True)
        when = started(p)[:16].replace("T", " ") or "?"
        print(f"  {mode:<20} {when}  first call {first:>7,}  tools {len(tools):>2} ≈{k(sum(t for t, _ in tools)):>6}"
              f"  biggest: " + ", ".join(f"{name} {k(t)}" for t, name in tools[:5]))
    if not shown:
        print("  no logged request shapes yet")


def find(agent, prefix):
    for p in agent.sessions():
        if agent.raw_id(p).startswith(prefix) or os.path.basename(p).startswith(prefix):
            return p
    sys.exit(f"no {agent.name} session starting with {prefix}")


NOTES = os.environ.get("BIGDAD_NOTES") or os.path.join(CLAUDE_DIR, "bigdad", "notes.jsonl")


def cmd_watch():
    """The plugin's watch hook sees this command and watches the rest of the session."""
    print("🥛 == WATCH  Big Dad is watching the rest of this session.")
    print("  Logged by the hook: tool outputs over ~5k tokens (tool + size, never content; Bash over 30k chars = its 2k preview).")
    print('  Noted by the agent: `bigdad.py note "what happened, what to change"`.')
    print("  Read back: `bigdad.py notes`. A note becomes a change only with the user's yes.")


def cmd_note(text):
    if not text.strip():
        sys.exit('usage: bigdad.py note "what happened, what to change"')
    os.makedirs(os.path.dirname(NOTES), exist_ok=True)
    with _open(NOTES, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "kind": "agent", "text": text.strip()}) + chr(10))
    print("noted.")


def cmd_notes(n=40):
    try:
        with _open(NOTES, encoding="utf-8", errors="replace") as f:
            rows = []
            for l in f:
                try:
                    d = json.loads(l)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        print("🥛 == NOTES  none yet. Run `bigdad.py watch` in a session to start.")
        return
    big = [r for r in rows if r.get("kind") == "big-output"]
    agent = [r for r in rows if r.get("kind") == "agent"]
    starts = sum(r.get("kind") == "watch-start" for r in rows)
    print(f"🥛 == NOTES  {starts} watched session(s), {len(big)} big outputs, {len(agent)} agent notes")
    if big:
        by = {}
        for r in big:
            t = by.setdefault(str(r.get("tool", "?")), [0, 0])
            t[0] += 1; t[1] += r.get("tokens") if isinstance(r.get("tokens"), (int, float)) else 0
        prev = sum(1 for r in big if r.get("kept_chars"))
        print("  Big outputs, by what the model kept (until /compact each rides along on every later call;")
        print(f"  Bash outputs over ~30k chars count as their ~2k-char preview: {prev} such). `reread` has the re-sent total:")
        for tool, (k, tok) in sorted(by.items(), key=lambda x: -x[1][1]):
            print(f"    {tool:<12} {k:>3} x  ~{tok:,} tokens")
        sub = {}
        for r in big:
            if r.get("agent"):
                t = sub.setdefault(str(r.get("agent_type") or "?"), [0, 0])
                t[0] += 1; t[1] += r.get("tokens") if isinstance(r.get("tokens"), (int, float)) else 0
        if sub:
            print("  of which in subagents (each rides along in that agent's own calls):")
            for kind, (k, tok) in sorted(sub.items(), key=lambda x: -x[1][1]):
                print(f"    {kind[:24]:<24} {k:>3} x  ~{tok:,} tokens")
    if agent:
        print("  Agent notes (newest last):")
        for r in agent[-n:]:
            ts = str(r.get("ts") or "?")[:16].replace("T", " ")
            print(f"    {ts}  {r.get('text', '')}")
    print("  Turn a note into a change only with the user's yes; then prove it and measure again.")


def cmd_receipt(rest):
    """The receipt for one chat: the newest chat by default, or a session id (or its start) or a log path."""
    import glob
    import bd_common as BC
    import bd_money as M
    arg = rest[0] if rest else ""
    if arg and os.path.isfile(arg):
        path = arg
    else:
        logs = sorted(glob.glob(os.path.join(BC.config_dir(), "projects", "*", "*.jsonl")), key=os.path.getmtime)
        hits = [p for p in logs if os.path.basename(p).startswith(arg)]
        if not hits:
            print("🥛 Big Dad: no chat log found" + (f" for {arg}" if arg else "") + ".")
            return
        path = hits[-1]
    sid = os.path.splitext(os.path.basename(path))[0]
    print(M.receipt_text(path, sid, sys.stdout.isatty() and not os.environ.get("NO_COLOR")))


def main():
    global REDACT
    args = sys.argv[1:]
    private = [w for w in os.environ.get("BIGDAD_PRIVATE", "").split(",") if w.strip()]
    if "--private" in args:
        i = args.index("--private")
        private += [w for w in (args[i + 1] if i + 1 < len(args) else "").split(",") if w.strip()]
        del args[i:i + 2]
    if "--redact" in args:
        args.remove("--redact")
        REDACT = True
        sys.stdout = Redact(sys.stdout, [w.strip() for w in private])
    cmd = args[0] if args else "all"
    rest = args[1:]
    if cmd in ("help", "-h", "--help"):
        print("🥛 Big Dad commands:\n" + (__doc__ or "").split("\n\n", 1)[-1].rstrip())
        return
    agents = [a for a in (Claude(),) if a.present()]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if cmd == "probe":
        cmd_probe(Claude(), rest)
        return
    if cmd == "agent-stop":
        import bd_common as BC
        f = BC.stop_file()
        v = rest[0].lower() if rest else ""
        if v in ("off", "0"):
            if os.path.exists(f):
                BC.remove_file(f)
            print("Big Dad hard stop: off. Agents still get the warning (every call past 1.5x the guide).")
            return
        kind, x = BC.parse_cap(v)
        if kind == "off":
            print("usage: bigdad.py agent-stop 100k  (on: cap every subagent at 100k tokens per call)\n"
                  "       bigdad.py agent-stop 2     (on: cap at 2x the guide)\n"
                  "       bigdad.py agent-stop off")
            cap = BC.stop_cap()
            print(f"now: {'on, ' + BC.stop_label(cap) if cap else 'off'}; guide {BC.guard_at() // 1000:,}k per call")
            return
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with BC.open_wait(f, "w", encoding="utf-8") as fh:
            fh.write((f"{x}" if kind == "tokens" else f"{x:g}") + "\n")
        cap = x if kind == "tokens" else int(x * BC.guard_at())
        what = f"{cap // 1000:,}k tokens per call" if kind == "tokens" else f"{x:g}x the guide ({cap // 1000:,}k per call)"
        print(f"Big Dad hard stop: on at {what}. At ~{int(BC.SOFT_AT * cap) // 1000:,}k a subagent is told to get its "
              f"handoff ready; past {cap // 1000:,}k it can only Write/Edit its handoff "
              f"({os.path.join(BC.bd_dir(), 'handoffs')}) and hand back, and the main chat gets one relaunch line. "
              "Never the main chat. Off: bigdad.py agent-stop off. "
              "Applies from the next tool call in any session with Big Dad's hooks loaded.")
        return
    if cmd == "sentinel":
        __import__("bd_sentinel").cmd_sentinel(rest)
        return
    if cmd == "statusline":
        __import__("bd_sentinel").cmd_statusline(rest)
        return
    if cmd == "pause":
        import bd_common as BC
        f = os.path.join(BC.bd_dir(), "pause-at")
        v = rest[0].lower() if rest else ""
        try:
            x = 0.0 if v in ("off", "0") else float(v)
        except ValueError:
            x = -1
        if x != 0 and x < 1:
            print("usage: bigdad.py pause 2     (end the turn once the main chat passes 2x the guide; interactive only)\n"
                  "       bigdad.py pause off")
            print(f"now: {'at ' + format(BC.pause_at(), 'g') + 'x' if BC.pause_at() else 'off'}; guide {BC.guard_at() // 1000:,}k per call")
            return
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with BC.open_wait(f, "w", encoding="utf-8") as fh:
            fh.write("off\n" if x == 0 else f"{x:g}\n")
        print("Big Dad pause: off. The guard still warns." if x == 0 else
              f"Big Dad pause: at {x:g}x the guide ({int(x * BC.guard_at()) // 1000:,}k per call), once per 1.5x step, "
              "interactive sessions only. Applies from the next tool call.")
        return
    if cmd == "helper-floor":
        import bd_common as BC
        f = os.path.join(BC.bd_dir(), "helper-floor")
        v = rest[0].strip().lower() if rest else ""
        os.environ.pop("BIGDAD_HELPER_FLOOR", None)
        kind = None
        if v in ("auto", "off"):
            kind = v
        elif v:
            os.environ["BIGDAD_HELPER_FLOOR"] = v
            k, n = BC.helper_floor()
            os.environ.pop("BIGDAD_HELPER_FLOOR", None)
            kind = f"{n}" if k == "tokens" else ("off" if k == "off" else None)
        if kind is None:
            print("usage: bigdad.py helper-floor auto   (default: a helper's re-read under its call size / calls so far goes through)\n"
                  "       bigdad.py helper-floor 4k     (a fixed floor in tokens)\n"
                  "       bigdad.py helper-floor off    (deny every re-read of an unchanged file, as before 1.7)")
            k, n = BC.helper_floor()
            print(f"now: {n:,} tokens" if k == "tokens" else f"now: {k}")
            return
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with BC.open_wait(f, "w", encoding="utf-8") as fh:
            fh.write(kind + "\n")
        if kind == "auto":
            say = "Big Dad helper floor: auto. A helper's re-read under its call size / calls so far goes through."
        elif kind == "off":
            say = "Big Dad helper floor: off. Every re-read of an unchanged file in context is denied, helpers too."
        else:
            say = f"Big Dad helper floor: {int(kind):,} tokens. A helper's re-read under that goes through."
        print(say + " The main chat is unchanged. BIGDAD_HELPER_FLOOR overrides this file.")
        return
    if cmd == "tidy":
        __import__("bd_tidy").cmd_tidy(rest)
        return
    if cmd == "shrink":
        __import__("shrink").cmd(rest)
        return
    if cmd == "outline":
        __import__("outline").cmd(rest)
        return
    if cmd == "keep":
        __import__("bd_keep").cmd(rest)
        return
    if cmd == "self":
        nums = [a for a in rest if not a.startswith("-")]
        __import__("bd_self").cmd_self(os.path.join(CLAUDE_DIR, "projects"), float(nums[0]) if nums else 14.0,
                                       as_json="--json" in rest)
        return
    if cmd == "receipt":
        cmd_receipt(rest)
        return
    if cmd == "versus":
        __import__("bd_versus").main(rest)
        return
    if cmd == "settle":
        import bd_settle
        return bd_settle.cmd_settle(rest)
    if cmd == "baseline":
        __import__("bd_baseline").cmd_baseline(rest)
        return
    if cmd == "setup":
        __import__("bd_setup").cmd_setup(measure="--no-measure" not in rest)
        return
    if cmd == "refresh":
        __import__("bd_refresh").cmd_refresh(rest)
        return
    if cmd in ("health", "fresh", "agents", "report", "models", "modes", "all"):
        __import__("bd_setup").ensure()          # Big Dad's own knobs: missing, 7 days old or new Claude Code -> re-tune
    if cmd == "health":
        __import__("bd_health").cmd_health()
        return
    if cmd == "fresh":
        __import__("bd_fresh").cmd_fresh()
        return
    if cmd == "agents":
        __import__("bd_agents").cmd_agents(float(rest[0]) if rest else 24)
        return
    if cmd == "report":
        __import__("bd_report").cmd_report(rest)
        return
    if cmd == "proof":
        if rest[:1] != ["read"]:
            sys.exit("usage: bigdad.py proof read [--model haiku]")
        __import__("bd_proof").cmd_proof_read(rest[1:])
        return
    if cmd == "files":
        cmd_files()
        return
    if cmd == "watch":
        cmd_watch()
        return
    if cmd == "note":
        cmd_note(" ".join(rest))
        return
    if cmd == "notes":
        cmd_notes(int(rest[0]) if rest and rest[0].isdigit() else 40)
        return
    if cmd not in ("load", "usage", "reread", "modes", "models", "tools", "skills", "compare", "all"):
        sys.exit(f"unknown command: {cmd} (see the top of this file)")
    if not agents:
        print("No Claude Code session logs found. Use Claude Code for a session, then rerun.")
        print(f"  looked in: {os.path.join(CLAUDE_DIR, 'projects')}")
        return
    n = int(rest[0]) if rest and rest[0].isdigit() else None
    for a in agents:
        if cmd == "load":
            a.load(find(a, rest[0]) if rest else None)
        elif cmd == "usage":
            a.usage(n or 10)
        elif cmd == "reread":
            a.reread(n)
        elif cmd == "modes":
            cmd_modes(a)
        elif cmd == "models":
            __import__("bd_models").cmd_models(a.proj, float(rest[0]) if rest else 24)
        elif cmd == "tools":
            cmd_tools(a, n or 2)
        elif cmd == "skills":
            a.skills()
        elif cmd == "compare":
            ra, rb = a.load_parts(find(a, rest[0])), a.load_parts(find(a, rest[1]))
            if ra and rb:
                print(f"🥛 == COMPARE [{a.name}] first-call tokens {rest[0]} -> {rest[1]}:"
                      f" {ra[0]:,} -> {rb[0]:,} ({rb[0]-ra[0]:+,})")
        elif cmd == "all":
            a.reread(n or 10)
            a.load()
            a.usage(n or 10)
            a.skills()
            __import__("bd_models").cmd_models(a.proj, 24)
        else:
            sys.exit(f"unknown command: {cmd} (see the top of this file)")
    if cmd == "all":
        cmd_files()
        print("🥛 == ALL covered: reread, load, usage, skills, models (24 h), files. Not run: modes, tools, health, fresh, notes.")


if __name__ == "__main__":
    if BC:
        BC.patient()                # a command waits out a file held open elsewhere longer than a hook does
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
    except OSError as e:            # a state file is a folder, read-only, locked: say which, no traceback
        name = os.path.basename(str(e.filename or "")) or "a file"
        if BC and isinstance(e, BC.Held):
            print(f"🥛 Big Dad: couldn't {e.what} {name}: another program kept it open for {e.waited:g} s. "
                  "Nothing was lost; try again in a moment.", file=sys.stderr)
        else:
            print(f"🥛 Big Dad: couldn't read or write {name} ({e.strerror or e}). Stopped there; fix or remove it "
                  "and run the command again.", file=sys.stderr)
        sys.exit(1)
