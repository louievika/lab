"""Big Dad receipt: what a chat sent and what each compact dropped, in tokens, read from the chat's own log.

Measured (from the log): every call's input tokens (new, written to cache for 5 min or 1 hour, re-read from cache)
and output tokens, one per message id; every drop (a call <=70% of the call before it, after a logged compact
boundary, or from past the guide to under it), labelled "compact" when Claude Code logged a compact boundary.
COMPACTS shows each drop as measured: the call before -> the call after, nothing multiplied out (1.7: the old
"saved = drop x later calls" total was an unstated formula, so it is gone). 🥛 marks the drops that ran with Big
Dad's slim instructions; the rest are Claude Code's own (auto or manual).
Plan users (Pro/Max): Claude Code gives the status line the 5-hour and weekly limits as % used (`rate_limits`); the
status line keeps them in live.json and the receipt shows them. Anthropic does not publish tokens per %, so the
receipt claims no % of the plan. No prices: tokens only.
Credit (counted, from rows Claude Code itself writes to the log): a compact is "slim compact" when the log holds the
PreCompact hook's own output with Big Dad's slim instructions (`<local-command-stdout>` right after the boundary);
a compact with no such row stays "compact" (plain Claude Code, e.g. before slim existed). Read-guard blocks are the
denied Read's tool_result (is_error), guard warnings are `hook_system_message` attachments,
pauses are `hook_stopped_continuation` attachments, output shrinks are the shrink note closing a tool_result (its
own before -> after estimate), outlines are the denied Read that carries outline-first's map.
Mentions inside Bash output or file text do not count.
Waste cut vs spent by choice (2.0.0): the did-list is headed WASTE CUT. When the keep list (bd_keep) names skills
or MCP servers, SPENT BY CHOICE prices each from this chat's own listing rows (skill_listing, mcp_instructions_delta,
deferred_tools_delta): its text / 4 x calls, an estimate, capped at the total sent. The two lines add up to the
total sent, so savings are never overstated and a choice is never called waste.
"""
import json, os, re, sys, textwrap, unicodedata
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

VERSION = 7                                             # status-line state: bumped when Tally gains fields
ANSI = re.compile("\033\\[[0-9;]*m")
SLIM_MARK = "Big Dad slim compact"                      # compact_slim.py's first line, as PreCompact stdout
DENIED = re.compile(r"PreToolUse:(Read|Bash|PowerShell) hook error")   # Claude Code's prefix on a hook-denied call
SLIM_DID = "cut raw output, stale reads, retries"
OUTLINE_MARK = "Outline first, so the whole file"      # outline.py's deny text
CHOICE_MARKS = (b'"skill_listing"', b'"mcp_instructions_delta"', b'"deferred_tools_delta"')
# raw-line prefilter: only lines that can carry a call, a boundary or a Big Dad mark are parsed
MARKS = (b'"usage"', b"compact_boundary", b"local-command-stdout", b"hook_system_message",
         b"hook_stopped_continuation", b"hook error", b"Big Dad shrink")
# the shrink note is a whole line near the end (Claude Code appends its own "Shell cwd was reset" line after it)
SHRINK_NOTE = re.compile(r"(?m)^\[\S* ?Big Dad shrink: showed [\d,]+ of [\d,]+ lines \(~([\d,]+) → ~([\d,]+) tokens.*\]\r?$")
RECEIPT_WORDS = ("receipt", "🧾", "bigdad receipt", "big dad receipt")
CHURN_AT = 3                            # this many logged compacts in one chat: the receipt adds the churn lines
KEEP = 40                               # drops and parts kept in the running state


def big(n):
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n))


def cells(s):
    """Display columns a terminal gives s: wide emoji 2, combining marks and ANSI codes 0, the rest 1."""
    return sum(0 if unicodedata.category(ch) in ("Mn", "Me", "Cf") else 2 if unicodedata.east_asian_width(ch) in ("W", "F")
               else 1 for ch in ANSI.sub("", s))


def ljust(s, n):
    """s padded on the right to n display columns (str.ljust counts 🥛 as 1; a terminal draws it as 2)."""
    return s + " " * max(0, n - cells(s))


def local(ts, fmt="%H:%M"):
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone().strftime(fmt)
    except (TypeError, ValueError):
        return "?"


class Tally:
    """One chat, added up call by call. JSON-able (to_dict) so the status line can carry it between refreshes."""
    INTS = ("calls", "new", "w5", "w1h", "read", "out", "over", "prev",
            "compacts", "slims", "blocks", "warns", "pauses", "peak", "shrinks", "shrink_cut", "outlines", "rewritten")

    def __init__(self, d=None, guide=130_000):
        d = d if isinstance(d, dict) else {}
        self.guide = guide
        for k in self.INTS:
            setattr(self, k, d[k] if isinstance(d.get(k), int) and not isinstance(d.get(k), bool) else 0)
        self.last = d.get("last") if isinstance(d.get("last"), str) else None
        self.boundary = d.get("boundary") if isinstance(d.get("boundary"), str) else None
        self.slim = d.get("slim") is True            # the pending boundary carried Big Dad's slim instructions
        self.first_ts = d.get("first_ts") if isinstance(d.get("first_ts"), str) else ""
        self.last_ts = d.get("last_ts") if isinstance(d.get("last_ts"), str) else ""
        self.models = {k: v for k, v in (d.get("models") or {}).items() if isinstance(v, int)} \
            if isinstance(d.get("models"), dict) else {}
        self.cuts = [c[:4] for c in d.get("cuts") or [] if isinstance(c, list) and len(c) in (4, 6)]  # ts label before after
        self.parts = [p for p in d.get("parts") or [] if isinstance(p, list) and len(p) == 4]

    @property
    def sent(self):
        return self.new + self.w5 + self.w1h + self.read

    @property
    def plain_compacts(self):
        return max(0, self.compacts - self.slims)

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.INTS}
        d.update(last=self.last, boundary=self.boundary, slim=self.slim, first_ts=self.first_ts, last_ts=self.last_ts,
                 models=self.models, cuts=self.cuts[-KEEP:], parts=self.parts[-KEEP:])
        return d

    def feed(self, raw):
        """One raw log line (bytes or str)."""
        if isinstance(raw, bytes):
            if not any(m in raw for m in MARKS):
                return
            raw = raw.decode("utf-8", "replace")
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        t = d.get("type")
        if t == "system" and d.get("subtype") == "compact_boundary":
            meta = d.get("compactMetadata") if isinstance(d.get("compactMetadata"), dict) else {}
            self.boundary = "auto-compact" if meta.get("trigger") == "auto" else "compact"
            self.slim = False
            self.compacts += 1
            return
        if t == "attachment":
            return self.credit_attachment(d.get("attachment"))
        if t == "user":
            return self.credit_user(d)
        self.add(d)

    def credit_attachment(self, a):
        """Rows Claude Code writes for a hook's own output: Big Dad's warnings and pauses."""
        if not isinstance(a, dict):
            return
        kind = a.get("type")
        if kind == "hook_system_message":
            text = C.dstr(a.get("content"))
            if "Big Dad" not in text or "receipt:" in text:
                return
            if "re-sends" in text and "new topic?" not in text:     # old 1.5 topic notes: not warnings
                self.warns += 1
        elif kind == "hook_stopped_continuation":
            text = C.dstr(a.get("message"))
            if "Big Dad" in text and "paused" in text:
                self.pauses += 1

    def credit_user(self, d):
        """User-side rows: the /compact command's stdout (holds the PreCompact hook's output) and denied tool calls."""
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        c = m.get("content")
        if isinstance(c, str):
            if c.lstrip().startswith("<local-command-stdout>") and "PreCompact" in c and SLIM_MARK in c:
                self.slims += 1
                self.slim = True
            return
        if not isinstance(c, list):
            return
        for block in c:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            text = inner if isinstance(inner, str) else "".join(
                x.get("text", "") for x in inner if isinstance(x, dict)) if isinstance(inner, list) else ""
            if block.get("is_error") is not True:
                end = text[-1500:]
                m = SHRINK_NOTE.search(end) if "Big Dad shrink" in end else None
                if m:                                  # output shrink: the note closes the tool result it cut
                    before, after = (int(g.replace(",", "")) for g in m.groups())
                    self.shrinks += 1
                    self.shrink_cut += max(0, before - after)
                continue
            if DENIED.match(text.lstrip()) and C.is_read_deny(text):    # Read, Bash and PowerShell re-reads
                self.blocks += 1
            elif DENIED.match(text.lstrip()) and OUTLINE_MARK in text:   # outline-first: the map, not the file
                self.outlines += 1

    def add(self, d):
        m = d.get("message") if d.get("type") == "assistant" else None
        u = m.get("usage") if isinstance(m, dict) else None
        if not isinstance(u, dict):
            return
        mid = m.get("id") or d.get("requestId")
        if mid and mid == self.last:
            return                      # Claude Code writes one row per content block of the same call
        try:
            new, cw, read, out = (int(u.get(k) or 0) for k in
                                  ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
            cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
            w1h = min(cw, int(cc.get("ephemeral_1h_input_tokens") or 0))
        except (TypeError, ValueError):
            return
        n = new + cw + read
        if n <= 0 or min(new, cw, read, out) < 0:
            return                      # error rows and synthetic rows carry no real call
        self.last = mid if isinstance(mid, str) else None
        ts = C.dstr(d.get("timestamp"))
        self.first_ts = self.first_ts or ts
        self.last_ts = ts
        model = C.dstr(m.get("model")) or "?"
        logged = bool(self.boundary) and self.prev > 0
        if logged or (n <= 0.7 * self.prev and self.prev >= self.guide and n < self.guide):
            # 2.0.0: a logged compact counts even under the guide (auto-compact can fire at ~117k on a 130k guide)
            # 2.0.5: and however little it dropped (a chat with a big fixed start compacts 115k -> 85k, over and
            # over: those were missing rows). The 30% rule stays only for a drop the log does not explain.
            label = ("slim " if self.slim and self.boundary else "") + (self.boundary or "drop")
            if logged:
                self.rewritten += new + cw      # the call right after a compact: what was not re-read from cache
            self.cuts.append([ts, label, self.prev, n])
            self.parts.append([ts, label, 0, 0])
        if not self.parts:
            self.parts.append([ts, "start", 0, 0])
        self.boundary, self.slim = None, False
        self.prev = n
        self.peak = max(self.peak, n)
        self.calls += 1
        self.new, self.w5, self.w1h, self.read, self.out = (self.new + new, self.w5 + cw - w1h, self.w1h + w1h,
                                                            self.read + read, self.out + out)
        self.models[model] = self.models.get(model, 0) + 1
        part = self.parts[-1]
        part[2] += 1
        part[3] += n
        over = max(0, n - self.guide)
        self.over += over


def read(path, guide=None):
    t = Tally(guide=guide or C.guard_at())
    try:
        with open(path, "rb") as f:
            for raw in f:
                t.feed(raw)
    except (OSError, TypeError):
        pass
    return t


def plan(sid):
    """Claude Code's own plan-limit numbers for this chat, as the status line last saw them (live.json)."""
    try:
        with open(os.path.join(C.bd_dir(), "live.json"), encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not sid or d.get("session") != sid[:8] or not isinstance(d.get("plan"), dict):
        return None
    return d["plan"]


def agents_of(path):
    """This chat's subagents, read from their own logs: [(type, id, Tally)]. Claude Code keeps them next to the chat:
    <project>/<session>/subagents/agent-<id>.jsonl (+ .meta.json with agentType), and workflow agents under
    <session>/subagents/workflows/<run>/agent-<id>.jsonl."""
    import glob
    base = os.path.splitext(path)[0] if path else ""
    if not base or not os.path.isdir(os.path.join(base, "subagents")):
        return []
    sub = os.path.join(glob.escape(base), "subagents")
    out = []
    for p in sorted(glob.glob(os.path.join(sub, "agent-*.jsonl")) + glob.glob(os.path.join(sub, "workflows", "*", "agent-*.jsonl"))):
        meta = C.load_json(p[:-6] + ".meta.json")
        kind = C.dstr(meta.get("agentType")) or ("workflow" if os.sep + "workflows" + os.sep in p else "agent")
        out.append((kind, os.path.basename(p)[6:-6], read(p)))
    return out


def chosen(path, kept=None):
    """Kept skills and MCP servers, priced from this chat's newest listing rows: [(kind, name, chars per call)].
    Settings and habits on the keep list have no row of their own in the log: counted, not priced."""
    try:
        import bd_keep
    except ImportError:
        return [], 0
    kept = bd_keep.items() if kept is None else kept
    if not kept:
        return [], 0
    skills, tools, blocks = {}, {}, {}
    try:
        with open(path, "rb") as f:
            for raw in f:
                if not any(m in raw for m in CHOICE_MARKS):
                    continue
                try:
                    a = json.loads(raw).get("attachment")
                except (ValueError, AttributeError):
                    continue
                if not isinstance(a, dict):
                    continue
                kind = a.get("type")
                if kind == "skill_listing" and isinstance(a.get("content"), str):
                    skills = {}                                 # the newest full listing wins
                    for line in ("\n" + a["content"]).split("\n- "):
                        name = line.split(": ", 1)[0].strip()
                        if name and " " not in name and line.strip():
                            skills[name] = len(line) + 3
                elif kind == "mcp_instructions_delta":      # deltas repeat after each compact: newest per server
                    for blk in a.get("addedBlocks") or []:
                        if isinstance(blk, str) and blk.startswith("## "):
                            blocks[blk[3:].split("\n", 1)[0].strip()] = len(blk)
                    for name in a.get("removedNames") or []:
                        blocks.pop(name, None)
                elif kind == "deferred_tools_delta":        # one entry per tool name, however often re-announced
                    for tool in a.get("addedLines") or a.get("addedNames") or []:
                        if isinstance(tool, str) and tool.startswith("mcp__") and "__" in tool[5:]:
                            tools[tool] = len(tool) + 1
                    for tool in a.get("removedNames") or []:
                        tools.pop(tool, None)
    except OSError:
        return [], 0
    out, hit = [], set()
    for name, chars in skills.items():
        key = bd_keep.match(name, "skill", kept) or bd_keep.match(name, "plugin", kept)
        if key:
            out.append(("skill", name, chars))
            hit.add(key)
    mcp = dict(blocks)
    for tool, chars in tools.items():
        name = tool[5:].split("__", 1)[0]
        mcp[name] = mcp.get(name, 0) + chars
    merged = {}
    for name, chars in mcp.items():                     # "claude.ai Vercel" (instructions) + "claude_ai_Vercel" (tools)
        key = bd_keep.match(name, "mcp", kept)
        if key:
            merged[key] = merged.get(key, 0) + chars
            hit.add(key)
    out += [("mcp", kept[k].get("name") or k, chars) for k, chars in merged.items()]
    unpriced = sum(1 for k in kept if k not in hit and k != "verbose")
    return sorted(out, key=lambda r: -r[2]), unpriced


def render(t, sid="", color=False, plan_info=None, agents=None, choice=None):
    g, r, y, d, b, e = ("\033[32m", "\033[1;31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m") if color else ("",) * 6
    W = 66                                                    # every column ends at the same place: 36/45/55/66
    pct = lambda before, after: f"{100 * (after - before) / before:+.0f}%" if before else ""
    model = max(t.models, key=t.models.get) if t.models else "?"
    when = f"{local(t.first_ts, '%m-%d %H:%M')} → {local(t.last_ts, '%H:%M')}" if t.first_ts else "no calls yet"
    rows = [f"{b}🥛 BIG DAD · RECEIPT 🧾{e}", f"{d}chat {sid[:8] or '?'} · {when} · {model}{e}", "═" * W]
    if not t.calls:
        return "\n".join(rows + ["  no API calls logged in this chat yet.", "═" * W])
    rows.append(f"{b}  WHAT RAN{e} {d}(measured){e}{'calls':>24}{'input':>10}")
    parts = t.parts[-8:]
    if len(t.parts) > 8:
        rows.append(f"{d}  ... {len(t.parts) - 8} earlier parts{e}")
    for i, (ts, label, calls, sent) in enumerate(parts, len(t.parts) - len(parts) + 1):
        rows.append(f"  {i:>2}  {local(ts)}  {label:<18}{calls:>14,}{big(sent):>10}")
    rows += ["─" * W, f"{b}  TOKENS{e}{'tokens':>47}"]
    for icon, label, n in (("🔁", "re-read from cache", t.read), ("📝", "written to cache, 1 hour", t.w1h),
                           ("📝", "written to cache, 5 min", t.w5), ("🆕", "new input", t.new),
                           ("📤", "output (Claude's replies)", t.out)):
        if n:
            rows.append(f"  {icon} {label:<34}{big(n):>16}")
    rows.append(f"  📈 {'peak call':<34}{big(t.peak):>16}")
    rows.append(f"     {'calls':<34}{t.calls:>16,}")
    rows.append(f"  {b}💸 TOTAL{e}{big(t.sent + t.out):>45}")
    rows += ["─" * W, f"{b}  COMPACTS{e} {d}(measured: the call before → after){e}{'per call':>12}"]
    if not t.cuts:
        rows.append("  no compact yet: every call re-sends the whole chat.")
    for ts, label, before, after in t.cuts[-6:]:
        drop = f"{big(before)} → {big(after)} {pct(before, after)}"
        mark = ljust("🥛" if label.startswith("slim ") else "", 2)     # a 2-column slot: 🥛 or two spaces
        diff = ("-" if before >= after else "+") + big(abs(before - after))
        rows.append(f"{mark}{local(ts)}  {label[:18]:<19}{drop:<21}{diff:>9}")
    if len(t.cuts) > 6:
        rows.append(f"{d}  ... {len(t.cuts) - 6} earlier drops{e}")
    if any(c[1].startswith("slim ") for c in t.cuts):
        rows.append(f"{d}  🥛 = Big Dad slim compact: {SLIM_DID}{e}")       # 65 cells: fits W
    logged = [c for c in t.cuts if "compact" in c[1]]
    if len(logged) >= CHURN_AT:                               # many compacts close together: say what they cost
        gaps = sorted(p[2] for p in t.parts[:-1])             # calls in each finished part
        apart = f", a median of {gaps[len(gaps) // 2]} calls apart" if gaps else ""
        rows.append(f"  churn: {t.compacts} compacts{apart}")
        if t.rewritten:
            rows.append(f"    written again, not re-read, on the call after each: {big(t.rewritten)}")
        rows.append(f"    first call after a compact: {big(logged[0][3])} at first → {big(logged[-1][3])} latest")
        rows.append(f"{d}    lever: a smaller fixed start, or a bigger autoCompactWindow{e}")
    s = lambda n: "s" if n != 1 else ""                       # every line below fits in W: text <= 57 chars
    if t.plain_compacts:                                      # a fact about a compact, not a cut: it stays here
        rows.append(f"{d}  {t.plain_compacts} compact{s(t.plain_compacts)} with no Big Dad slim row in the log: not credited{e}")
    did = [(t.slims, "🥛", f"slim compact{s(t.slims)}: {SLIM_DID}"),
           (t.blocks, "🚫", f"double read{s(t.blocks)} blocked: the text was already in context"),
           (t.shrinks, "📉", f"big output{s(t.shrinks)} shrunk: ~{big(t.shrink_cut)} tokens cut, full copy kept"),
           (t.outlines, "🧭", f"outline{s(t.outlines)} first: a big file's map, not the whole file"),
           (t.warns, "🟡", f"guard warning{s(t.warns)}: a call re-sent more than the {t.guide // 1000}k guide"),
           (t.pauses, "🛑", f"pause{s(t.pauses)}: turn stopped far past the guide, to realign")]
    if any(n for n, _, _ in did):
        rows += ["─" * W, f"{b}  WASTE CUT{e} {d}(what Big Dad did, counted from this chat's log){e}"]
        rows += [f"  {icon} {n:>3}  {text}" for n, icon, text in did if n]
    priced, unpriced = choice or ([], 0)
    if priced or unpriced:
        rows += ["─" * W, f"{b}  SPENT BY CHOICE{e} {d}(your keep list, est.: text / 4 x calls){e}"]
        by_choice = 0
        for kind, name, chars in priced[:6]:
            n = chars // 4 * t.calls
            by_choice += n
            rows.append(f"  ⭐ {name[:28]:<29}{kind:<6}{'≈' + f'{chars // 4:,}' + '/call':>13}{big(n):>13}")
        for kind, name, chars in priced[6:]:
            by_choice += chars // 4 * t.calls
        if len(priced) > 6:
            rows.append(f"{d}  ... {len(priced) - 6} more kept, counted below{e}")
        if unpriced:
            rows.append(f"{d}  ⭐ {unpriced} kept setting{s(unpriced)}/habit{s(unpriced)}: no row of their own in the log{e}")
        by_choice = min(by_choice, t.sent)
        rows.append(f"  by choice ≈{big(by_choice)} · the rest {big(t.sent - by_choice)} of {big(t.sent)}"
                    f" {d}input sent{e}")
    agents = [a for a in agents or [] if a[2].calls]
    a_sent = sum(a[2].sent for a in agents)
    a_out = sum(a[2].out for a in agents)
    if agents:
        rows += ["─" * W, f"{b}  AGENTS{e} {d}(their own logs){e}{'calls':>14}{'re-sent':>9}{'peak':>7}{'output':>11}"]
        for kind, aid, at in sorted(agents, key=lambda a: -a[2].sent)[:8]:
            m = max(at.models, key=at.models.get) if at.models else "?"
            label = f"{kind[:12]} {aid[:8]} {m.replace('claude-', '')[:8]}"
            rows.append(f"  {label:<30}{at.calls:>7,}{big(at.sent):>9}{big(at.peak):>7}{big(at.out):>11}")
        if len(agents) > 8:
            rows.append(f"{d}  ... {len(agents) - 8} more agents, counted in the total{e}")
        rows.append(f"  {b}{ljust(f'🚚 {len(agents)} AGENT' + ('S' if len(agents) != 1 else ''), 30)}{e}"
                    f"{sum(a[2].calls for a in agents):>7,}{big(a_sent):>9}{'':>7}{b}{big(a_out):>11}{e}")
        if max(a[2].peak for a in agents) >= t.guide:
            rows.append(f"{y}  agent passed the {t.guide // 1000}k guide: autoCompactWindow, then agent-stop{e}")
    rows.append("═" * W)
    if agents:
        chat, sub = t.sent + t.out, a_sent + a_out
        rows.append(f"  {b}🧾 this chat + its agents: {big(chat + sub)} tokens{e}")
        rows.append(f"{d}     chat {big(chat)} · agents {big(sub)} ({100 * sub / max(chat + sub, 1):.0f}%){e}")
    if t.over:
        rows.append(f"  {r}🔴 {big(t.over)} re-sent past the {t.guide // 1000}k guide: /compact sooner{e}")
    bits = []
    for k, name in (("five_hour", "5-hour"), ("seven_day", "weekly")):
        v = (plan_info or {}).get(k)
        if isinstance(v, dict) and isinstance(v.get("used_percentage"), (int, float)):
            bits.append(f"{name} limit {v['used_percentage']:.0f}% used")
    if bits:
        rows.append(f"  📊 plan: {' · '.join(bits)} {d}(Claude Code's numbers){e}")
    note = ("Tokens only: no prices, and no % of the plan claimed. Each compact shows the measured call "
            "before → after; no total is multiplied out. Type receipt anytime.")
    rows += [f"{d}  {line}{e}" for line in textwrap.wrap(note, W - 2)]
    return "\n".join(rows)


def receipt_text(path, sid="", color=False):
    return render(read(path), sid, color, plan(sid), agents_of(path), chosen(path))
