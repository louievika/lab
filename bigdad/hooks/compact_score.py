#!/usr/bin/env python3
"""Big Dad scored compact: rank what the chat holds since the last compact, keep the top items word for word.

A PreCompact command hook cannot edit or replace the summary (docs: hooks, PreCompact output: stdout becomes
instructions for the summary, 10,000-char cap; PostCompact has no decision control). So "kept verbatim" here
means: the items are quoted in the instructions and the summary is told to copy them as they are, then to keep
everything else short. (Replacing the summary with the original messages, as fast-jev-compaction does, needs
Claude Code's early-access function hooks, `session.compact`, TypeScript; not a command hook.)

Scores (higher survives first; the budget is characters of this hook's stdout):
  pinned  the user's latest asks, their rules and constraints (never/always/don't/must/secret...), the
          user's own /compact text: always quoted, newest first, never dropped for a lower item
  80      open todos (TodoWrite, TaskCreate without a finished TaskUpdate)
  70-90   open errors (a failed call with no later success of the same call), newest first
  60-90   files being edited (Edit/Write/MultiEdit/NotebookEdit, shell writes), newest first
  45-75   decisions the assistant stated, commits made
  low     stale reads, re-reads, big raw outputs, fixed errors, finished agents: named in DROP, never quoted

Secrets: a value that looks like a secret (sk-..., ghp_..., AKIA..., a long key, `password: x`) is replaced by
[redacted] in the quotes, so the rule survives and the value is not copied a second time. Pure stdlib; every
public function is called inside a try/except by compact_slim, which falls back to the old text on any error.
"""
import json, os, re

PIN_ASKS = 3           # the user's newest asks, always quoted
ASK_MAX = 1_200        # chars of the newest ask quoted; older pinned asks get OLD_ASK_MAX
OLD_ASK_MAX = 500
RULE_MAX = 300         # one rule sentence
RULES_MAX = 6         # rule sentences from older asks
RULE_ASKS = 200        # older asks searched for those rules, newest first
FILES_MAX = 12
ERRORS_MAX = 5
DECISIONS_MAX = 4
COMMITS_MAX = 5
SUMMARY_WORDS = 450   # the summary's own text, on top of the quotes (tuned on the replay benchmark, see README)
RULE_SCAN = 200_000    # chars of one ask searched for rule sentences (splitting a 40 MB paste takes seconds)

RULE = re.compile(r"\b(never|always|don'?t|do not|must(?:n'?t)?|from now on|remember|only ever|no more|"
                  r"stop (?:using|doing)|avoid|password|passcode|secret|credential|api[ _-]?key|private|"
                  r"confidential|do not share|keep it (?:off|out)|not allowed|forbidden)\b", re.I)
DECIDE = re.compile(r"\b(decided|decision|going with|we'?ll use|i'?ll use|chose|instead of|the plan is|root cause|"
                    r"the fix is|switching to|settled on)\b", re.I)
COMMIT = re.compile(r"^\[([\w./-]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.{1,90})", re.M)
TRACE = re.compile(r"Traceback \(most recent call last\)|\bFAILED \(|^\s*(?:fatal|error)(?:\[\w+\])?:", re.M)
SECRET = [
    re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|[sr]k_(?:live|test)_[A-Za-z0-9]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|"
               r"github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
               r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9._-]+)"),
    # `password is x`, `"password": "x"`, `token=x`: strong words hide any value; pin/code only one with a digit
    re.compile(r"(?i)\b(?:(password|passcode|passwd|passphrase|token|secret|api[ _-]?key|access[ _-]?key)|(pin|code))"
               r"([\"']?\s*(?:is|=|:)\s*)([\"']?)((?(1)[^\s\"',;]{3,}|(?=[^\s\"',;]*\d)[^\s\"',;]{3,}))"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}(?![\w/.\\-])"),
    re.compile(r"(://[^\s:/@]+:)([^\s@/]+)(@)"),            # a password inside a URL: postgres://app:s3cret@db
]
# hook and command markup Claude Code writes as user rows: not the user's ask (a pasted <Button> is one)
SKIP_PROMPT = ("<task-notification", "<command-name", "<command-message", "<command-args", "<local-command",
               "<bash-input", "<bash-stdout", "<bash-stderr", "<user-prompt-submit-hook", "[Request interrupted",
               "Caveat:", "This session is being continued")
WORDY = re.compile(r"[A-Za-z][a-z0-9_.-]*|[0-9a-z_.-]+")


def _long_token(m):
    """A 40+ char token: a path (after a root, or every part a plain word) or a hash stays; anything else is a key."""
    t, before = m.group(0), m.string[max(0, m.start() - 1):m.start()]
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", t):
        return t                                             # a git or sha256 hash, not a secret
    words = [x for x in re.split(r"[_-]+", t) if x]
    if len(words) >= 3 and all(re.fullmatch(r"[A-Za-z][a-z]*|[A-Z]{2,5}|\d{1,4}", x) for x in words):
        return t                                             # plain words joined: mcp__claude_ai_Google_Drive__search
    parts = [x for x in re.split(r"[\\/]", t) if x]
    if len(parts) > 1 and (before in ("/", "\\", ":", "~", ".") or all(WORDY.fullmatch(x) for x in parts)):
        return t
    return "[redacted]"


def redact(text):
    """The text with secret-looking values replaced by [redacted]; the words around them stay."""
    text = SECRET[0].sub("[redacted]", str(text))
    text = SECRET[1].sub(lambda m: (m.group(1) or m.group(2)) + m.group(3) + m.group(4) + "[redacted]"
                         if not m.group(5).startswith("[redacted") else m.group(0), text)
    text = SECRET[3].sub(lambda m: m.group(1) + "[redacted]" + m.group(3), text)
    return SECRET[2].sub(_long_token, text)


def clamp16(text, cap):
    """text cut to `cap` UTF-16 units (Claude Code's count), never splitting a pair; a lone surrogate (which would
    make the hook's output fail to encode) becomes U+FFFD first."""
    text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if len(text.encode("utf-16-le")) // 2 <= cap:
        return text
    out, n = [], 0
    for ch in text:
        n += 2 if ord(ch) > 0xFFFF else 1
        if n > cap:
            break
        out.append(ch)
    return "".join(out)


def one_line(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _blocks(d):
    m = d.get("message")
    return m.get("content") if isinstance(m, dict) else None


def _strip_reminders(s):
    return re.sub(r"<system-reminder>.*?</system-reminder>", " ", s, flags=re.S).strip()


def prompt_text(d):
    """The user's own typed words in a transcript row, or "" (tool results, hook text, commands, meta rows skipped)."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary") or d.get("toolUseResult") is not None:
        return ""
    c = _blocks(d)
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return ""
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str):
        return ""
    c = _strip_reminders(c)
    if not c or c.lstrip().startswith(SKIP_PROMPT) or re.match(r"/[\w:-]+\s*$", c.strip()):
        return ""                      # a bare slash command (/compact, /clear) is not an ask
    return c


def rule_sentences(text):
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n+", text[:RULE_SCAN]):
        s = " ".join(s.strip(" -*•\t").split())
        m = RULE.search(s) if len(s) >= 8 else None
        if m and len(s) > RULE_MAX:        # dictated text runs on without stops: keep the words around the rule
            a = max(0, m.start() - 60)
            a = s.rfind(" ", 0, a) + 1 if a else 0
            s = ("…" if a else "") + s[a:]
        if m:
            out.append(one_line(s, RULE_MAX))
    return out


NO_EXT = {"Makefile", "Dockerfile", "LICENSE", "README", "CHANGELOG", "Procfile", "Gemfile", "Rakefile", "Justfile"}


def looks_like_file(p):
    """A shell-write target worth naming: a path or a name with an extension, no quotes or brackets; its last part
    is a file name with an extension (or Makefile-like, or a dotfile), and no `:` past a drive letter (code or words
    a parser took for a path: `sed 's/a/dad/'` names no file "dad")."""
    p = str(p or "").strip()
    base = re.split(r"[\\/]", p.rstrip("\\/"))[-1]
    return (3 <= len(p) <= 260 and not re.search(r"[\"'()<>|`$]", p) and not p.endswith((",", ";"))
            and bool(re.search(r"[\\/]|\.\w{1,8}$", p)) and len(base) >= 2 and not re.search(r"[=*?]", base)
            and ":" not in (p[2:] if re.match(r"[A-Za-z]:[\\/]", p) else p) and not re.fullmatch(r"[\d.]+", base)
            and bool(re.search(r"\.(?=\w*[A-Za-z])\w{1,8}$", base) or base in NO_EXT or re.fullmatch(r"\.[\w.-]+", base)))


def older_asks(path, tail, rows, cap=96 << 20, chunk=4 << 20):
    """When the scanned tail (the last `tail` bytes) holds fewer than PIN_ASKS of the user's asks and no compact
    boundary, read further back (up to `cap` bytes, stopping at a boundary) for the newest missing ones. Only
    lines that look like user prompts are parsed. Returns those rows, oldest first."""
    need = PIN_ASKS
    for d in reversed(rows):
        if need <= 0:
            return []
        if isinstance(d, dict) and prompt_text(d):
            need -= 1
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell() - tail
        if need <= 0 or pos <= 0:
            return []
        f.seek(pos - 1)
        cut = f.read(1) != b"\n"            # the scan skipped the partial line at pos; its end belongs back here
        tail_bytes = f.read(tail)
        if b'"compact_boundary"' in tail_bytes:
            return []
        nl = tail_bytes.find(b"\n")
        carry = tail_bytes[:nl + 1] if cut and nl >= 0 else (tail_bytes if cut else b"")
        found, stop = [], max(0, pos - cap)
        while pos > stop and len(found) < need:
            start = max(stop, pos - chunk)
            f.seek(start)
            buf = f.read(pos - start) + carry
            pos = start
            lines = buf.split(b"\n")
            carry = lines.pop(0) if start > 0 else b""
            for line in reversed(lines):
                if b'"compact_boundary"' in line and b'"subtype"' in line:
                    return list(reversed(found))
                if b'"type":"user"' not in line or b'"tool_result"' in line:
                    continue
                try:
                    d = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    continue
                if isinstance(d, dict) and prompt_text(d):
                    found.append(d)
                    if len(found) >= need:
                        break
    return list(reversed(found))


NAMED_ERR = re.compile(r"\b\w+(?:Error|Exception)\b|\berror\[E\d+\]")
SOME_ERR = re.compile(r"(?i)\b(?:error|failed|fatal|denied|refused|cannot|can't|not found|no such file|timed? ?out|"
                      r"traceback|permission|forbidden|unauthori[sz]ed|panicked)\b")


def error_line(out):
    """The line that names a failure: the last one with a named error (a traceback's end), else the first one
    with an error word, else the first real line."""
    lines = [l for l in str(out).splitlines()[:400] if l.strip() and not re.match(r"Exit code \d+$|\[exited with code", l)]
    named = [l for l in lines if NAMED_ERR.search(l)]
    if named:
        return named[-1]
    return next((l for l in lines if SOME_ERR.search(l)), lines[0] if lines else str(out)[:200])


def _key(name, inp):
    """What makes two calls 'the same call' for error tracking: the tool and its main argument."""
    v = inp.get("command") or inp.get("file_path") or inp.get("notebook_path") or inp.get("pattern") or inp.get("url") or ""
    return name + ":" + " ".join(str(v).split())[:80]


def _label(name, inp):
    if name in ("Bash", "PowerShell"):
        return f"{name} `{one_line(inp.get('command', ''), 70)}`"
    v = inp.get("file_path") or inp.get("notebook_path") or inp.get("pattern") or inp.get("url") or inp.get("description") or ""
    return f"{name} {one_line(v, 70)}".strip()


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return ""


def _short_path(p, cwd):
    try:
        if cwd and os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(os.path.abspath(cwd)) + os.sep):
            return os.path.relpath(p, cwd)
    except (ValueError, TypeError):
        pass
    return p


def collect(rows, writes_of=None, is_deny=None):
    """Score the rows of one compact segment. Returns a dict of lists, each newest first:
    asks [(idx, text)], rules [text], files [(score, path, edits, op)], errors [(score, label, line)],
    fixed (count), todos [text], decisions [(score, text)], commits [(score, text)], last (assistant's last words)."""
    n = max(1, len(rows))
    asks, calls, result_of, todos, tasks, decisions, commits = [], {}, {}, [], {}, [], []
    files, last = {}, ""
    for i, d in enumerate(rows):
        if not isinstance(d, dict) or d.get("type") == "api-request-blob":    # a copy of rows already seen
            continue
        t = prompt_text(d)
        if t:
            asks.append((i, t))
            continue
        c = _blocks(d)
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "tool_use":
                if b.get("id") and b.get("id") in calls:                     # the same call logged twice: count it once
                    continue
                name = str(b.get("name") or "")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else ""
                calls[b.get("id")] = (i, name, inp)
                fp = inp.get("file_path") or inp.get("notebook_path")
                targets, tool_edit = [], name in ("Edit", "Write", "MultiEdit", "NotebookEdit")
                if tool_edit and isinstance(fp, str) and fp:
                    targets = [fp]
                elif name in ("Bash", "PowerShell") and writes_of:
                    try:
                        targets = sorted(p for p in writes_of(str(inp.get("command") or ""), cwd) if looks_like_file(p))
                    except Exception:
                        targets = []
                for p in targets:
                    k = os.path.normcase(p)
                    old = files.get(k, (0, p, 0, "", False))
                    files[k] = (i, _short_path(p, cwd), old[2] + 1, name, old[4] or tool_edit)
                if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    todos = [one_line(x.get("content") or x.get("activeForm") or "", 160) for x in inp["todos"]
                             if isinstance(x, dict) and x.get("status") != "completed"]
                elif name == "TaskCreate":
                    tasks[str(len(tasks) + 1)] = one_line(inp.get("subject") or inp.get("description") or "", 160)
                elif name == "TaskUpdate" and inp.get("status") in ("completed", "deleted"):
                    tasks.pop(str(inp.get("taskId") or ""), None)
            elif kind == "tool_result":
                result_of[b.get("tool_use_id")] = (i, bool(b.get("is_error")), _text(b.get("content")))
            elif kind == "text" and d.get("type") == "assistant":
                txt = str(b.get("text") or "").strip()
                if txt:
                    last = txt
                    for s in re.split(r"(?<=[.!?])\s+|\n+", txt):
                        if DECIDE.search(s) and len(s) > 20 and not s.rstrip(" *_").endswith("?"):
                            decisions.append((45 + 30 * i / n, one_line(s, 220)))
    # errors: a failed call is open until the same call succeeds later
    by_key, errors, fixed = {}, [], 0
    for tid, (i, name, inp) in sorted(calls.items(), key=lambda kv: kv[1][0]):
        r = result_of.get(tid)
        if not r:
            continue
        _, err, out = r
        if name in ("Bash", "PowerShell") and not err:
            for m in COMMIT.finditer(out[:4000]):
                commits.append((40 + 20 * i / n, f"{m.group(2)[:9]} {one_line(m.group(3), 80)} ({m.group(1)})"))
        bad = err or bool(TRACE.search(out[:3000]))
        if bad and (is_deny and is_deny(out) or "doesn't want to proceed" in out or "was rejected" in out[:200]):
            bad = False                       # a Big Dad deny or the user's own no: not an error to fix
        k = _key(name, inp)
        if bad:
            by_key.pop(k, None)               # re-insert: newest failure last
            by_key[k] = (i, _label(name, inp), one_line(error_line(out), 200))
        elif k in by_key:
            del by_key[k]
            fixed += 1
    for k, (i, lb, line) in by_key.items():
        errors.append((70 + 20 * i / n, lb, line))
    errors.sort(key=lambda x: -x[0])
    rules, seen = [], set()
    for _, t in reversed(asks[-RULE_ASKS - PIN_ASKS:-PIN_ASKS] if len(asks) > PIN_ASKS else []):
        if len(rules) >= RULES_MAX:
            break
        if not RULE.search(t[:RULE_SCAN]):
            continue
        for s in rule_sentences(t):
            if s.lower() not in seen:
                seen.add(s.lower())
                rules.append(s)
    return {
        "asks": list(reversed(asks[-PIN_ASKS:])),
        "rules": rules[:RULES_MAX],
        "files": sorted((((60 + 30 * i / n) if ed else (30 + 25 * i / n), p, e, op) for i, p, e, op, ed in files.values()),
                        key=lambda x: -x[0])[:FILES_MAX],
        "errors": errors[:ERRORS_MAX], "fixed": fixed,
        "todos": [t for t in todos if t] + [t for t in tasks.values() if t],
        "decisions": sorted(decisions, key=lambda x: -x[0])[:DECISIONS_MAX],
        "commits": sorted(commits, key=lambda x: -x[0])[:COMMITS_MAX],
        "last": one_line(last, 400) if last else "",
        "rows": len(rows),
    }


def _rule_list(s):
    """Rule sentences to quote: from the cut-off part of a long pinned ask, then from older asks; at most
    RULES_MAX + 2, no repeats."""
    out, seen = [], set()
    cut = []
    for j, (_, t) in enumerate(s.get("asks") or []):
        cap = ASK_MAX if j == 0 else OLD_ASK_MAX
        q, rest = _head(t, cap, secret=False)
        if rest:
            cut += [r for r in rule_sentences(t) if r[:60] not in q]
    for r in cut + list(s.get("rules") or []):
        if len(out) >= RULES_MAX + 2:
            break
        if r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def _head(t, cap, secret=True):
    """The first `cap` chars of an ask, whitespace collapsed (and redacted), and how many chars are left after
    them (0 when it all fits). Only the start is normalised, so a 40 MB paste costs no more than a short one."""
    raw = t[:cap * 4 + 256]
    q = " ".join(raw.split())
    if secret:
        q = redact(q)                  # before the cut, so a key that spans it is still caught whole
    return q[:cap], max(0, len(q) - cap) + len(t) - len(raw)


def rules_wanted(s):
    return len(_rule_list(s))


def u16(text):
    """Length in UTF-16 units, the way Claude Code counts the 10,000-char cap."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def block(s, room):
    """The MUST SURVIVE lines from collect()'s dict, in priority order, inside `room` characters (pinned first).
    Returns (lines, kept) where kept counts what made it in per kind."""
    out, kept, used = [], {}, 0

    def add(kind, line, pinned=False):
        nonlocal used
        line = redact(line)
        if not pinned and used + len(line) + 1 > room:
            return False
        out.append(line)
        used += len(line) + 1
        kept[kind] = kept.get(kind, 0) + 1
        return True

    for j, (_, t) in enumerate(s.get("asks") or []):   # the asks first and always: nothing below can push one out
        cap = ASK_MAX if j == 0 else OLD_ASK_MAX
        q, rest = _head(t, cap)
        more = f" […the rest of this message ({rest:,} more chars): copy it in full from the chat]" if rest else ""
        add("asks", f"> ASK ({'newest' if j == 0 else f'{j + 1} back'}, user's words): \"{q}\"{more}", pinned=True)
    for r in _rule_list(s):               # then the rules, before anything ranked
        add("rules", f"> RULE (user's words): \"{r}\"")
    ranked = [(80, "todos", f"• TODO open: {t}") for t in s.get("todos") or []]
    ranked += [(sc, "errors", f"• OPEN ERROR: {lb} -> {redact(line)}") for sc, lb, line in s.get("errors") or []]
    ranked += [(sc, "files", f"• FILE in progress: {p} ({e} edit{'s' if e != 1 else ''}, last {op})") for sc, p, e, op in s.get("files") or []]
    ranked += [(sc, "decisions", f"• DECISION: {redact(t)}") for sc, t in s.get("decisions") or []]
    ranked += [(sc, "commits", f"• COMMIT: {t}") for sc, t in s.get("commits") or []]
    if s.get("last"):
        ranked.append((85, "last", f"• STOPPED AT (assistant's last words): {redact(s['last'])}"))
    ranked.sort(key=lambda x: -x[0])
    for _, kind, line in ranked:
        add(kind, line)
    return out, kept


def size_rule(s):
    return (f"SIZE: quote the MUST SURVIVE lines as they are, then write at most ~{SUMMARY_WORDS} more words. No code blocks, "
            "no file contents, no tool output, no diffs: a path and one line per file. One line per finished step. "
            "Older asks that are done: one line each, not quoted. Skip any section that would only repeat the lines above.")


def receipt(s, kept, kept_chars, dead):
    """Itemised before -> after estimate (tokens = chars // 4), the lines `--show` prints."""
    parts = ", ".join(f"{v} {k}" for k, v in kept.items()) or "nothing"
    rows = [f"[receipt] scored compact, estimated (chars/4, not measured):",
            f"  kept word for word  ~{kept_chars // 4:,} tokens ({parts})",
            f"  fixed errors named  {s.get('fixed', 0)}"]
    for label, n in dead:
        rows.append(f"  drop  {label:<34} ~{n // 4:,} tokens")
    rows.append(f"  summary budget      ~{SUMMARY_WORDS} words (~{SUMMARY_WORDS * 4 // 3:,} tokens) + the quotes")
    return rows
