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
Short and full (3.0.0): the receipt shown by default (typed `receipt`, the reprint after a compact, `bigdad.py
receipt`) is the short one, render_short: 44 columns, at most 18 lines, the totals, the plan limits, the last cut,
Big Dad's counts and the warnings, every figure one the full receipt shows with the same meaning. `receipt full`
(or `bigdad.py receipt --full`) is the full one, render, with every section above.
"""
import json, os, re, stat, sys, textwrap, time, unicodedata
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C

VERSION = 8                                             # status-line state: bumped when Tally gains fields
ANSI = C.SGR                                            # thirty-eighth review: bd_common's pattern, written once
_TOK = re.compile(ANSI.pattern + "|.", re.S)            # thirty-ninth review: fit()'s tokens (an SGR or one char)
RESET = "\033[0m"
W = 66                                                  # the receipt's width in terminal columns: no line is wider
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


def clip(s, n):
    """s cut to at most n display columns, control characters dropped (names come from logs: no escape codes).
    Thirty-seventh review: the set is bd_common's _CONTROL (the shown() rule), so U+2028/2029 and the bidi controls
    are dropped too, not only category Cc."""
    out, w = "", 0
    for ch in str(s):
        if ch in C._CONTROL:
            continue
        c = cells(ch)
        if w + c > n:
            break
        out, w = out + ch, w + c
    return out


def fit(line, width=W):
    """One receipt line as lines of at most width columns, by rule: words wrap, a word wider than the line is cut,
    the next line is indented 2 past the first. Colour codes take no columns, so the breaks are the same with colour
    on or off; a colour open at a break is reset there and opened again on the next line (no colour crosses a
    newline: the in-chat receipt goes out as one joined message)."""
    if cells(line) <= width:
        return [line]
    plain = ANSI.sub("", line)
    indent = " " * min(width // 2, len(plain) - len(plain.lstrip(" ")) + 2)
    out, cur = [], ""
    for word in line.split(" "):
        joined = f"{cur} {word}" if out or cur else word
        if cells(joined) <= width:
            cur = joined
            continue
        if ANSI.sub("", cur).strip():
            out.append(cur)
            cur = indent + word
        else:
            cur = joined
        while cells(cur) > width:                     # a path or a run of emoji with no space: cut by columns
            head, n = "", 0
            for tok in _TOK.findall(cur):            # thirty-eighth review: bd_common's SGR (compiled once)
                n += cells(tok)
                if n > width:
                    break
                head += tok
            out.append(head)
            cur = indent + cur[len(head):]
    out.append(cur)
    lines, carry = [], ""
    for piece in out:
        codes = ANSI.findall(piece)
        lead = len(piece) - len(piece.lstrip(" "))
        piece = piece[:lead] + carry + piece[lead:]
        state = carry + "".join(codes)
        carry = state[state.rfind(RESET) + len(RESET):] if RESET in state else state
        lines.append(piece + RESET if carry else piece)
    return lines


def want_color(stream=None, flag=None, env=None):
    """The one colour rule for the receipt (3.0.0). flag is the command line: --no-color (False) or --color (True)
    wins. Then NO_COLOR set and not empty: plain. Then FORCE_COLOR or CLICOLOR_FORCE set and not 0: colour. Then
    colour only on a TTY whose TERM is not "dumb". Anything unclear is plain."""
    if flag is not None:
        return bool(flag)
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    if any(env.get(k, "").strip().lower() not in ("", "0", "false", "off", "no") for k in ("FORCE_COLOR", "CLICOLOR_FORCE")):
        return True
    if env.get("TERM", "").strip().lower() == "dumb":
        return False
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def chat_color(env=None):
    """Colour for the receipt shown inside Claude Code (typed `receipt`, the reprint after a compact):
      BIGDAD_RECEIPT_COLOR=off   plain (the default: whether Claude Code draws ANSI in a hook message is undocumented)
      BIGDAD_RECEIPT_COLOR=on    the coloured receipt
      BIGDAD_RECEIPT_COLOR=auto  want_color's rule; a hook's stdout is no TTY, so on only with FORCE_COLOR/CLICOLOR_FORCE
    Any other value is off."""
    env = os.environ if env is None else env
    v = env.get("BIGDAD_RECEIPT_COLOR", "").strip().lower()
    if v in ("on", "1", "true", "yes"):
        return True
    return v == "auto" and want_color(None, None, env)


def local(ts, fmt="%H:%M"):
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone().strftime(fmt)
    except (TypeError, ValueError, OverflowError, OSError):   # fortieth review: astimezone raised OSError on Windows
        return "?"                                            # for a 1969, year-1 or year-9999 time


class Tally:
    """One chat, added up call by call. JSON-able (to_dict) so the status line can carry it between refreshes."""
    INTS = ("calls", "new", "w5", "w1h", "read", "out", "over", "prev",
            "compacts", "slims", "blocks", "warns", "pauses", "peak", "shrinks", "shrink_cut", "outlines", "rewritten")

    def __init__(self, d=None, guide=130_000):
        d = d if isinstance(d, dict) else {}
        self.guide = guide
        for k in self.INTS:
            setattr(self, k, C.sane(d.get(k), whole=True))   # 32nd review: huge or negative reads as 0 too
        self.last = d.get("last") if isinstance(d.get("last"), str) else None
        # 3.0.0: every message id counted so far (Claude Code writes one row per content block of a call, and a log
        # can carry an earlier call again later): an id counts once per log, however far apart the repeats are
        self.seen = {x for x in d.get("seen") or [] if isinstance(x, str)} if isinstance(d.get("seen"), list) else set()
        if self.last:
            self.seen.add(self.last)
        self.boundary = d.get("boundary") if isinstance(d.get("boundary"), str) else None
        self.slim = d.get("slim") is True            # the pending boundary carried Big Dad's slim instructions
        self.first_ts = d.get("first_ts") if isinstance(d.get("first_ts"), str) else ""
        self.last_ts = d.get("last_ts") if isinstance(d.get("last_ts"), str) else ""
        self.models = {k: v for k, v in (d.get("models") or {}).items() if C.sane_ok(v, whole=True)} \
            if isinstance(d.get("models"), dict) else {}
        # thirty-first review: a cut or part is kept only as [str ts, str label, int, int]; a damaged one (the status
        # line's burn file: a str count raised at part[2] += 1, a number label at .startswith) is dropped
        row = lambda c: (isinstance(c[0], str) and isinstance(c[1], str)
                         and all(C.sane_ok(v, whole=True) for v in c[2:4]))   # 32nd: in range
        # a "cuts" or "parts" that is not a list (true, -5, NaN) reads as empty instead of raising TypeError
        rows = lambda k: d.get(k) if isinstance(d.get(k), list) else []
        self.cuts = [c[:4] for c in rows("cuts") if isinstance(c, list) and len(c) in (4, 6) and row(c)]  # ts label before after
        self.parts = [p for p in rows("parts") if isinstance(p, list) and len(p) == 4 and row(p)]

    @property
    def sent(self):
        return self.new + self.w5 + self.w1h + self.read

    @property
    def plain_compacts(self):
        return max(0, self.compacts - self.slims)

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.INTS}
        d.update(last=self.last, seen=sorted(self.seen), boundary=self.boundary, slim=self.slim, first_ts=self.first_ts, last_ts=self.last_ts,
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
        except (ValueError, RecursionError):
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
        # a list or dict id from a broken row cannot be a set key: the first str of the two, else no id
        mid = C.dstr(m.get("id")) or C.dstr(d.get("requestId")) or None
        if mid and mid in self.seen:
            return                      # one row per content block of the same call, or the call again later
        raw = [u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                  "output_tokens")]
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0 for v in raw):
            return                      # error rows and synthetic rows carry no real call
        # thirty-eighth review: each count through bd_common.sane (an int, not bool, 0..10**12); a str, float, bool,
        # NaN, Infinity (1e400 in a log) or huge count reads as 0 (int(inf) raised OverflowError), the row still counts
        new, cw, read, out = (C.sane(v, 0, whole=True) for v in raw)
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        w1h = min(cw, C.sane(cc.get("ephemeral_1h_input_tokens"), 0, whole=True))
        n = new + cw + read
        if n <= 0:
            return                      # error rows and synthetic rows carry no real call
        self.last = mid if isinstance(mid, str) else None
        if self.last:
            self.seen.add(self.last)
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


LIVE_MAX_AGE_S = 30 * 60   # a live.json the status line has not refreshed for this long is not shown as current
LIVE_CLOCK_SLACK_S = 60    # a live.json dated further than this in the future is not shown as current either
LIVE_MAX_BYTES = 1 << 20   # ninth review: a live.json bigger than this is not read (the status line writes ~200 bytes)
SHOWN_LIMITS = ("five_hour", "seven_day")   # the limits render and render_short show (with a number)
PCT_MAX = 1000             # tenth review: a used_percentage above this (or below 0) is not shown


def _reset_passed(v, now):
    """True when a limit's resets_at (epoch seconds, as the status line writes it) is at or before now; a missing
    or not understood value leaves the limit in place."""
    at = v.get("resets_at") if isinstance(v, dict) else None
    return C.is_number(at) and at <= now


def limit_shown(v):
    """True when a limit is one the receipts render: a dict with a used_percentage that is a number (not a bool).
    Tenth review: and from 0 to PCT_MAX inclusive, compared as it is (an int is never turned into a float first, so
    10**400 is simply out of range, never an OverflowError); inf and NaN fail the same comparison. The status line
    writes only what this passes (statusline.plan_of), so writer and reader share the rule."""
    pc = v.get("used_percentage") if isinstance(v, dict) else None
    return C.is_number(pc) and 0 <= pc <= PCT_MAX


def _read_live(p):
    """(bytes, mtime) of live.json, or None. Ninth review: plan() asks nothing about the folder, which another local
    user may be able to write to, so the read itself is guarded: one os.open (O_NOFOLLOW, O_NONBLOCK, O_BINARY where
    the platform has them: a link is not followed on POSIX, a FIFO does not block), then os.fstat on that descriptor:
    a regular file, (POSIX) owned by this user, at most LIVE_MAX_BYTES, its mtime from that same fstat. Windows opens
    through links, so it first refuses a link or other reparse point (lstat, C.entry_problem) and then a descriptor
    that is not the file it checked. Reads only: no write, no folder, no mode change."""
    checked = None
    try:
        if os.name == "nt":
            checked = os.lstat(p)
            if C.entry_problem(checked):
                return None
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0))
    except (OSError, TypeError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > LIVE_MAX_BYTES \
                or C.entry_problem(st, None if os.name == "nt" else os.geteuid()):   # bd_common's owner rule
            return None
        if checked is not None and (checked.st_dev, checked.st_ino) != (st.st_dev, st.st_ino):
            return None
        raw = b""
        while len(raw) <= LIVE_MAX_BYTES:
            got = os.read(fd, LIVE_MAX_BYTES + 1 - len(raw))
            if not got:
                break
            raw += got
        return None if len(raw) > LIVE_MAX_BYTES else (raw, st.st_mtime)
    except OSError:
        return None
    finally:
        os.close(fd)


def plan(sid):
    """Claude Code's own plan-limit numbers for this chat, as the status line last saw them (live.json).
    3.0.0 seventh review: a receipt must be factual, and the plan figure is whatever Claude Code last reported, so a
    reading whose file is older than LIVE_MAX_AGE_S is not shown as current, whatever stopped the refresh (the state
    folder refused, "Big Dad OFF", no tokens yet). A reading younger than that is at most 30 minutes old, as in any
    quiet chat. plan() asks nothing about the state folder: it makes no folder, changes no mode, and the idle status
    line still leaves live.json alone. Eighth review: a file dated more than LIVE_CLOCK_SLACK_S in the future is not
    current either, and a limit whose resets_at has passed is left out of the copy returned (that reading is from
    before the reset). Ninth review: the file is read through _read_live (one guarded open, its age from that
    descriptor; anything it refuses is None), and the copy is returned only while a limit the receipts render is
    left in it (SHOWN_LIMITS, with a number); other keys come back only alongside one. Otherwise None.
    Eleventh review: a shown limit that fails limit_shown is left out of the copy, so plan() is the one gate."""
    got = _read_live(os.path.join(C.bd_dir(), "live.json"))
    if got is None:
        return None
    raw, mtime = got
    now = time.time()
    age = now - mtime
    if age > LIVE_MAX_AGE_S or age < -LIVE_CLOCK_SLACK_S:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):           # tenth review: deep nesting under the cap is refused too
        return None
    if not isinstance(d, dict) or not sid or d.get("session") != sid[:8] or not isinstance(d.get("plan"), dict):
        return None
    left = {k: v for k, v in d["plan"].items()
            if not _reset_passed(v, now) and (k not in SHOWN_LIMITS or limit_shown(v))}
    return left if any(k in left for k in SHOWN_LIMITS) else None


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
        meta = C.load_json_now(p[:-6] + ".meta.json")   # twenty-fourth review: no waiting on a held file
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
                except (ValueError, AttributeError, RecursionError):
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
    # 3.0.0: the terminal's own text colour, with a light overlay: green on a measured saving (a compact's drop, a
    # counted Big Dad action), yellow on a warning, bold red only beside a PROBLEM or REGRESSION label, bold for heads
    # and totals, dim for side notes that also say what they are. Every coloured span closes on its own line.
    g, r, y, d, b, e = ("\033[32m", "\033[1;31m", "\033[33m", "\033[2m", "\033[1m", RESET) if color else ("",) * 6
    paint = lambda text, code: f"{code}{text}{e}" if code else text
    right = lambda text, n, code="": " " * max(0, n - cells(text)) + paint(text, code)   # right-aligned in n columns
    num = lambda n, width: f"{n:,}" if len(f"{n:,}") < width else big(n)                 # a count that fits its column
    total = lambda label, fig: f"{b}{ljust(label, W - cells(fig))}{fig}{e}"              # label left, figure right
    pct = lambda before, after: f"{100 * (after - before) / before:+.0f}%" if before else ""
    model = max(t.models, key=t.models.get) if t.models else "?"
    when = f"{local(t.first_ts, '%m-%d %H:%M')} → {local(t.last_ts, '%H:%M')}" if t.first_ts else "no calls yet"
    head = f"chat {clip(sid, 8) or '?'} · {when} · "
    rows = [f"{b}🥛 BIG DAD · RECEIPT 🧾{e}", f"{d}{head}{clip(model, W - cells(head))}{e}", "═" * W]
    if not t.calls:
        return "\n".join(rows + ["  no API calls logged in this chat yet.", "═" * W])
    # every section's last figure ends at the receipt's right edge, W; the columns before it at 36/45/55
    rows.append(f"{b}  WHAT RAN{e} {d}(measured){e}{'calls':>34}{'input':>11}")
    parts = t.parts[-8:]
    if len(t.parts) > 8:
        rows.append(f"{d}  ... {len(t.parts) - 8} earlier parts{e}")
    iw = max(2, len(str(len(t.parts))))
    for i, (ts, label, calls, sent) in enumerate(parts, len(t.parts) - len(parts) + 1):
        rows.append(f"  {i:>{iw}}  {local(ts)}  {ljust(clip(label, 20 - iw), 20 - iw)}{right(num(calls, 24), 24)}"
                    f"{big(sent):>11}")
    rows += ["─" * W, f"{b}  TOKENS{e}{'tokens':>58}"]
    for icon, label, n in (("🔁", "re-read from cache", t.read), ("📝", "written to cache, 1 hour", t.w1h),
                           ("📝", "written to cache, 5 min", t.w5), ("🆕", "new input", t.new),
                           ("📤", "output (Claude's replies)", t.out)):
        if n:
            rows.append(f"  {icon} {label:<34}{big(n):>27}")
    rows.append(f"  📈 {'peak call':<34}{big(t.peak):>27}")
    rows.append(f"     {'calls':<34}{right(num(t.calls, 27), 27)}")
    rows += ["─" * W, total("  💸 TOTAL", big(t.sent + t.out))]
    rows += ["─" * W, f"{b}  COMPACTS{e} {d}(measured: the call before → after){e}{'per call':>20}"]
    if not t.cuts:
        rows.append("  no compact yet: every call re-sends the whole chat.")
    for ts, label, before, after in t.cuts[-6:]:
        drop = f"{big(before)} → {big(after)} {pct(before, after)}"
        drop = drop if cells(drop) <= 20 else f"{big(before)} → {big(after)}"
        mark = ljust("🥛" if label.startswith("slim ") else "", 2)     # a 2-column slot: 🥛 or two spaces
        diff = ("-" if before >= after else "+") + big(abs(before - after))
        rows.append(f"{mark}{local(ts)}  {ljust(clip(label, 18), 27)}{drop:<21}"
                    f"{right(diff, 9, g if after < before else '')}")
        if after > before:                                    # a logged compact the next call came back bigger from
            rows.append(f"         {r}REGRESSION: the call after it was {big(after - before)} bigger{e}")
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
    s = lambda n: "s" if n != 1 else ""                       # longer lines wrap by fit(), never past W
    if t.plain_compacts:                                      # a fact about a compact, not a cut: it stays here
        rows.append(f"{d}  {t.plain_compacts} compact{s(t.plain_compacts)} with no Big Dad slim row in the log: not credited{e}")
    did = [(t.slims, "🥛", f"slim compact{s(t.slims)}: {SLIM_DID}", g),
           (t.blocks, "🚫", f"double read{s(t.blocks)} blocked: the text was already in context", g),
           (t.shrinks, "📉", f"big output{s(t.shrinks)} shrunk: ~{big(t.shrink_cut)} tokens cut, full copy kept", g),
           (t.outlines, "🧭", f"outline{s(t.outlines)} first: a big file's map, not the whole file", g),
           (t.warns, "🟡", f"guard warning{s(t.warns)}: a call re-sent more than the {t.guide // 1000}k guide", y),
           (t.pauses, "🛑", f"pause{s(t.pauses)}: turn stopped far past the guide, to realign", y)]
    if any(n for n, _, _, _ in did):
        rows += ["─" * W, f"{b}  WASTE CUT{e} {d}(what Big Dad did, counted from this chat's log){e}"]
        rows += [f"  {icon} {right(str(n), 3, code)}  {text}" for n, icon, text, code in did if n]
    priced, unpriced = choice or ([], 0)
    if priced or unpriced:
        rows += ["─" * W, f"{b}  SPENT BY CHOICE{e} {d}(your keep list, est.: text / 4 x calls){e}"]
        by_choice = 0
        for kind, name, chars in priced[:6]:
            n = chars // 4 * t.calls
            by_choice += n
            per = f"≈{chars // 4:,}/call" if len(f"≈{chars // 4:,}/call") < 13 else f"≈{big(chars // 4)}/call"
            rows.append(f"  ⭐ {ljust(clip(name, 28), 29)}{ljust(clip(kind, 5), 6)}{per:>13}{big(n):>13}")
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
            label = f"{clip(kind, 12)} {clip(aid, 8)} {clip(m.replace('claude-', ''), 8)}"
            rows.append(f"  {ljust(clip(label, 30), 30)}{right(num(at.calls, 7), 7)}{big(at.sent):>9}{big(at.peak):>7}"
                        f"{big(at.out):>11}")
        if len(agents) > 8:
            rows.append(f"{d}  ... {len(agents) - 8} more agents, counted in the total{e}")
        rows.append("─" * W)
        rows.append(f"  {b}{ljust(f'🚚 {len(agents)} AGENT' + ('S' if len(agents) != 1 else ''), 30)}"
                    f"{right(num(sum(a[2].calls for a in agents), 7), 7)}{big(a_sent):>9}{'':>7}{big(a_out):>11}{e}")
        if max(a[2].peak for a in agents) >= t.guide:
            rows.append(f"  {y}WARNING: an agent passed the {t.guide // 1000}k guide{e}")
            rows.append(f"{d}    lever: autoCompactWindow, then agent-stop{e}")
    rows.append("═" * W)
    if agents:
        chat, sub = t.sent + t.out, a_sent + a_out
        rows.append(total("  🧾 this chat + its agents", f"{big(chat + sub)} tokens"))
        rows.append(f"{d}     chat {big(chat)} · agents {big(sub)} ({100 * sub / max(chat + sub, 1):.0f}%){e}")
    if t.over:
        rows.append(f"  {r}🔴 PROBLEM: {big(t.over)} re-sent past the {t.guide // 1000}k guide: /compact sooner{e}")
    bits = []
    for k, name in zip(SHOWN_LIMITS, ("5-hour", "weekly")):
        v = (plan_info or {}).get(k)
        if limit_shown(v):                                    # ninth review: the same test as plan() and the short one
            bits.append(f"{name} limit {v['used_percentage']:.0f}% used")
    if bits:                                                  # both limits and the source: two lines (2.0.9 was 80)
        line, src = f"  📊 plan: {' · '.join(bits)}", f"{d}(Claude Code's numbers){e}"
        rows += [f"{line} {src}"] if cells(line) + 1 + cells(src) <= W else [line, f"     {src}"]
    note = ("Tokens only: no prices, and no % of the plan claimed. Each compact shows the measured call "
            "before → after; no total is multiplied out. Type receipt anytime.")
    rows += [f"{d}  {line}{e}" for line in textwrap.wrap(note, W - 2)]
    return "\n".join(piece for row in rows for piece in fit(row))


SW, SHORT_MAX = 44, 18                  # the short receipt (3.0.0 default): 44 columns, never more than 18 lines
SHORT_AIM = 14                          # a normal chat's short receipt: the fitting steps run until it is this long


def first_fit(*options, width=SW):
    """The first wording that fits width columns; the last one cut to width if none does."""
    for o in options:
        if cells(o) <= width:
            return o
    return clip(options[-1], width)


def render_short(t, sid="", color=False, plan_info=None, agents=None):
    """The short receipt (3.0.0, shown by default): the totals, the plan limits, the last cut, Big Dad's counts and
    the warnings, at most SW columns and SHORT_MAX lines. Every figure on it is one render() shows with the same
    meaning; nothing is priced, multiplied out or added up beyond what render() adds up. `receipt full` is render()."""
    b, g, y, r, e = ("\033[1m", "\033[32m", "\033[33m", "\033[1;31m", RESET) if color else ("",) * 5
    # colour runs from just after a line's two-space indent to its end, and closes on that line
    paint = lambda text, code: f"{text[:2]}{code}{text[2:]}{e}" if code else text
    num = lambda n, width: f"{n:,}" if len(f"{n:,}") < width else big(n)                 # as render() counts them
    s = lambda n: "s" if n != 1 else ""
    when = f"{local(t.first_ts, '%m-%d %H:%M')} → {local(t.last_ts, '%H:%M')}" if t.first_ts else "no calls yet"
    head = [f"{b}🥛 BIG DAD · RECEIPT 🧾{e}", first_fit(f"chat {clip(sid, 8) or '?'} · {when}"), "═" * SW]
    if not t.calls:
        return "\n".join(head + ["  no API calls logged in this chat yet.", "═" * SW])

    def spend(label, n, calls, fig, code=""):                 # label 20, calls right in 13, tokens right in 11
        calls = f"{calls} call{s(n)}"
        label = ljust(clip(label, 20), 20)
        return paint(first_fit(f"{label}{calls:>13}{fig:>11}", f"{label}{fig:>11}"), code)

    def chat_calls(n):                                        # render()'s calls row, or its short form if too wide
        fig = num(n, 27)
        return fig if len(fig) <= 7 else big(n)
    spends = []
    agents = [a for a in agents or [] if a[2].calls]
    chat = t.sent + t.out
    if agents:
        sub = sum(a[2].sent + a[2].out for a in agents)
        a_calls = sum(a[2].calls for a in agents)
        spends.append(spend("  💬 this chat", t.calls, chat_calls(t.calls), big(chat)))
        spends.append(spend(f"  🚚 {len(agents)} agent{s(len(agents))}", a_calls, num(a_calls, 7), big(sub)))
        fig = big(chat + sub)
        spends.append(paint(first_fit(*(f"{ljust(label, SW - 1 - cells(fig))} {fig}" for label in (
            "  💸 TOTAL · this chat + its agents", "  💸 TOTAL · chat + agents", "  💸 TOTAL"))), b))
    else:
        spends.append(spend("  💸 TOTAL", t.calls, chat_calls(t.calls), big(chat), b))
    bits = {}
    for k, name in zip(SHOWN_LIMITS, ("5-hour", "weekly")):
        v = (plan_info or {}).get(k)
        if limit_shown(v):
            bits[name] = f"{v['used_percentage']:.0f}%"
    if bits:
        a, w = bits.get("5-hour"), bits.get("weekly")
        if a and w:
            spends.append(first_fit(f"  📊 5-hour limit {a} used · weekly {w} used",
                                    f"  📊 5-hour limit {a} used · weekly {w}", f"  📊 5-hour {a} used · weekly {w} used",
                                    f"  📊 5-hour {a} · weekly {w}"))
        else:
            name, pc = ("5-hour", a) if a else ("weekly", w)
            spends.append(first_fit(f"  📊 {name} limit {pc} used", f"  📊 {name} {pc}"))

    cut = []                                                  # the last cut, and earlier ones that went up
    if not t.cuts:
        cut.append("  no compact yet: each call re-sends it all")
    else:
        _, label, before, after = t.cuts[-1]
        name = "compact" if "compact" in label else "drop"
        move = f"{big(before)} → {big(after)}"
        if after > before:
            cut.append(paint(first_fit(f"  🔴 REGRESSION: last {name} {move}", f"  🔴 REGRESSION: last {move}",
                                       f"  🔴 REGRESSION: {move}"), r))
        else:
            pct = f"{100 * (after - before) / before:+.0f}%" if before else ""
            full = f"{move} {pct}" if pct and cells(f"{move} {pct}") <= 20 else move     # render()'s own rule
            diff = "-" + big(before - after)
            edge = lambda left: f"{ljust(left, SW - 1 - cells(diff))} {diff}"
            code, mark = (g, "🟢 ") if after < before else ("", "")          # the same size: a fact, no mark
            lead = f"  {mark}last {name}  "
            cut.append(paint(first_fit(edge(lead + full), edge(lead + move), lead + full, lead + move,
                                       f"  {mark}{move}"), code))
        ups = sum(1 for c in t.cuts[-6:-1] if c[3] > c[2])
        if ups:
            cut.append(paint(first_fit(f"  🔴 REGRESSION: {ups} earlier compact{s(ups)} went up",
                                       f"  🔴 REGRESSION: {ups} earlier went up"), r))
    # Big Dad's counts: (the line's own wording, the wording when packed several to a line); SW - 5 is after "  🟢 "
    counts = [text for n, text in (
        (t.shrinks, (f"{t.shrinks} big output{s(t.shrinks)} shrunk: ~{big(t.shrink_cut)} cut",
                     f"{t.shrinks} shrunk: ~{big(t.shrink_cut)} cut")),
        (t.blocks, (f"{t.blocks} re-read{s(t.blocks)} blocked",) * 2),
        (t.outlines, (f"{t.outlines} big file{s(t.outlines)} outlined, not read whole",
                      f"{t.outlines} file{s(t.outlines)} outlined")),
        (t.slims, (f"{t.slims} slim compact{s(t.slims)}",) * 2)) if n > 0]

    warn = []
    if t.warns > 0 or t.pauses > 0:
        said = [x for n, x in ((t.warns, f"{t.warns} guard warning{s(t.warns)}"),
                               (t.pauses, f"{t.pauses} turn{s(t.pauses)} paused")) if n > 0]
        brief = [x for n, x in ((t.warns, f"{t.warns} warning{s(t.warns)}"), (t.pauses, f"{t.pauses} paused")) if n > 0]
        warn.append(paint(first_fit("  🟡 " + " · ".join(said), "  🟡 " + " · ".join(brief)), y))
    past = sum(1 for a in agents if a[2].peak >= t.guide)
    k = f"{t.guide // 1000}k"
    if past:
        warn.append(paint(first_fit(f"  🟡 WARNING: {past} agent{s(past)} passed the {k} guide",
                                    f"  🟡 WARNING: {past} agent{s(past)} over the {k} guide",
                                    f"  🟡 WARNING: {past} agent{s(past)} over the guide"), y))
    if t.over:
        warn.append(paint(first_fit(f"  🔴 PROBLEM: {big(t.over)} re-sent past the {k} guide",
                                    f"  🔴 PROBLEM: {big(t.over)} re-sent past {k} guide",
                                    f"  🔴 PROBLEM: {big(t.over)} re-sent past the guide",
                                    f"  🔴 PROBLEM: {big(t.over)} past the guide"), r))

    def pack(items):                                          # several counts to a line, joined with " · ": each
        out = []                                              # goes on the first line it fits
        for x in (first_fit(c[1], width=SW - 5) for c in items):
            i = next((i for i, line in enumerate(out) if cells(f"{line} · {x}") <= SW - 5), None)
            if i is None:
                out.append(x)
            else:
                out[i] = f"{out[i]} · {x}"
        return out

    foot = ["  From this chat's log. Tokens, not prices.", "  `receipt full` shows every line."]
    green, divide = [first_fit(*c, width=SW - 5) for c in counts], bool(warn)
    # over SHORT_AIM lines (a normal chat), the steps in this order until it fits; with every step taken the most it
    # can hold is SHORT_MAX: head 3, spend 3, plan 1, rule 1, cut 2, counts 4, warnings 3, closing rule 1
    for step in range(5):
        if step == 1:
            green = pack(counts)
        elif step == 2:
            foot = foot[1:]
        elif step == 3:
            divide = False
        elif step == 4:
            foot = []
        rows = (head + spends + ["─" * SW] + cut + [paint(first_fit("  🟢 " + x), g) for x in green]
                + (["─" * SW] if divide else []) + warn + ["═" * SW] + foot)
        if len(rows) <= SHORT_AIM:
            break
    return "\n".join(rows)


def receipt_text(path, sid="", color=False, full=True):
    """The full receipt (render) by default; full=False is the short one shown in chat and by `bigdad.py receipt`."""
    if full:
        return render(read(path), sid, color, plan(sid), agents_of(path), chosen(path))
    return render_short(read(path), sid, color, plan(sid), agents_of(path))
