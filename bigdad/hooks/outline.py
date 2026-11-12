"""Outline-first read (2.0.0, called by the read guard on PreToolUse Read).

The first whole-file Read of a big text file gets an outline instead (defs or headings with line numbers, size,
a grep hint). Asking for the whole file again goes through. An offset/limit Read is never touched, and a partial
Read still lets Edit work.

Where it runs (audit/LEDGER.md row 7, 14 days of logs): in helper agents it pays (code 32 of 33 big reads, docs
43 of 70). In the main chat it loses (median 3 calls left, so the extra call costs more than it saves). So:
  auto (default)  helpers only (main chat: a one-line note on a 40 KB+ first read, 2.0.1)
  on   helpers + main chat        off   never
`bigdad.py outline auto|on|off`, or BIGDAD_OUTLINE. Every outline is logged to outlines.jsonl so `self` can price it
(a later whole-file read of the same file counts against it).
"""
import json
import os
import re

import bd_common as C

MIN_BYTES = 24_000            # ~6k tokens: the size where the logs show a whole-file read starts to cost
NOTE_BYTES = 40_000           # ~10k tokens: main chat (auto) gets a one-line note, never a deny (2.0.1)
READ_MAX_BYTES = 256 * 1024   # Claude Code refuses a whole Read past this ("exceeds maximum allowed size (256KB)",
                              # seen live) unless allow_large is set: the note must not call that a read
MAX_ITEMS = 40
SKIP_EXT = re.compile(r"(?i)\.(png|jpe?g|gif|webp|bmp|ico|pdf|ipynb|svg|lock|min\.js|map)$")
DEF = re.compile(r"^\s{0,8}(?:export\s+|pub(?:\([^)]*\))?\s+|async\s+|static\s+|public\s+|private\s+|protected\s+)*"
                 r"(?:def|class|function|func|fn|struct|enum|impl|trait|interface|type|module|namespace|signal|"
                 r"const\s+\w+\s*=\s*(?:async\s*)?\(|@export)\b")
HEAD = re.compile(r"^(#{1,4} \S|\[[^\]]+\]\s*$)")     # markdown headings, ini/toml sections


def mode():
    v = os.environ.get("BIGDAD_OUTLINE", "").lower()
    if not v:
        try:
            with open(os.path.join(C.bd_dir(), "outline"), encoding="utf-8", errors="replace") as f:
                v = f.read().strip().lower()
        except OSError:
            v = ""
    if v in ("0", "false"):
        v = "off"
    try:
        if __import__("bd_keep").verbose():           # `keep verbose`: the user chose whole files
            return "off"
    except Exception:
        pass
    return v if v in ("auto", "on", "off") else "auto"


def outline(lines):
    """[(line_no, text)] of defs/headings, at most MAX_ITEMS, evenly thinned when there are more."""
    items = [(i + 1, s.rstrip()[:100]) for i, s in enumerate(lines) if DEF.match(s) or HEAD.match(s)]
    if len(items) > MAX_ITEMS:
        step = len(items) / MAX_ITEMS
        items = [items[int(k * step)] for k in range(MAX_ITEMS)]
    return items


def check(event):
    """A deny reason (str) for the first whole-file Read of a big text file, else None. Fails open."""
    return _first_big_read(event, note=False)


def big_note(event):
    """Main chat in auto mode: the first whole-file Read of a 40 KB+ text file goes through with a one-line note
    (no extra call, so it can't lose like the outline does there). Once per file per chat. Fails open."""
    return _first_big_read(event, note=True)


def _first_big_read(event, note):
    try:
        m = mode()
        agent = C.dstr(event.get("agent_id"))
        if event.get("tool_name") != "Read" or m == "off":
            return None
        if note != (m == "auto" and not agent):
            return None                                   # notes: auto main chat only; outlines: everywhere else on
        args = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        fp = C.dstr(args.get("file_path"))
        if not fp or any(args.get(k) is not None for k in ("offset", "limit", "pages")) or SKIP_EXT.search(fp):
            return None
        size = os.path.getsize(fp)
        if size < (NOTE_BYTES if note else MIN_BYTES) or size > (8 << 20):
            return None
        with open(fp, "rb") as f:
            raw = f.read()
        if b"\0" in raw[:4096]:
            return None                                   # binary
        lines = raw.decode("utf-8", "replace").splitlines()
        items = outline(lines)
        if len(items) < 3:
            return None                                   # nothing to steer by: an outline would only cost a call
        name = os.path.basename(fp) or fp
        if C.learning():
            C.learn_seen("big_read")                      # learning first (2.0.6): counted; no outline, note or row
            return None
        if note and size > READ_MAX_BYTES and not args.get("allow_large"):
            return (f"🥛 Big Dad: {name} is {len(lines):,} lines (~{size // 4:,} tokens), over the 256 KB Read limit, so "
                    "this Read is refused and reads nothing. Grep its headings or defs first and Read only the part you "
                    "need (offset/limit).")               # 2.0.5: not marked as noted, no row: nothing came in
        sid = re.sub(r"[^A-Za-z0-9-]", "", C.dstr(event.get("session_id")) or "x")[:64]
        st = os.path.join(C.state_dir(), f"bigdad-outline-{sid}-{re.sub(r'[^A-Za-z0-9-]', '', agent)[:40] or 'main'}.json")
        key = os.path.normcase(os.path.abspath(fp))
        with C.lock(st, wait=0.2):
            d = C.load_json(st)
            done = d.get("files") if isinstance(d.get("files"), list) else []
            if key in done:
                return None                               # asked again: the whole file goes through
            try:
                with open(st, "w", encoding="utf-8") as f:
                    json.dump({"files": done + [key]}, f)
            except OSError:
                return None
        if note:
            text = (f"🥛 Big Dad: {name} is {len(lines):,} lines (~{size // 4:,} tokens), read whole; it rides along on "
                    "every later call now. For the next big file, Grep its headings or defs first and Read only the "
                    "part you need (offset/limit).")
            C.append_jsonl(os.path.join(C.bd_dir(), "outlines.jsonl"),
                           {"ts": C.now(), "session": sid[:8], "agent": "", "file": name, "kind": "note",
                            "tokens": size // 4, "outline_tokens": 0})
            return text
        body = "\n".join(f"{n:>6}  {t.strip()}" for n, t in items)
        text = (f"🥛 Big Dad: {name} is {len(lines):,} lines (~{size // 4:,} tokens). Outline first, so the whole file "
                f"doesn't ride along on every later call:\n{body}\nRead the part you need with offset/limit, or Grep it. "
                "A partial Read is enough for Edit. Need it all? Read it again and it goes through.")
        C.append_jsonl(os.path.join(C.bd_dir(), "outlines.jsonl"),
                       {"ts": C.now(), "session": sid[:8], "agent": agent[:17], "file": name,
                        "tokens": size // 4, "outline_tokens": len(text) // 4})
        return text
    except Exception:
        return None


def cmd(rest):
    v = (rest[0] if rest else "status").lower()
    f = os.path.join(C.bd_dir(), "outline")
    if v in ("auto", "on", "off"):
        tmp = f + ".tmp"
        try:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            if os.path.isdir(f):
                raise OSError("outline is a folder, not a file")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(v + "\n")
            C.replace_file(tmp, f)       # the read guard reads it: held open (Windows) is waited out
        except OSError as e:
            print(f"🥛 Big Dad: couldn't save the outline mode ({e}). Nothing changed.", file=os.sys.stderr)
            raise SystemExit(1)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        print(f"🥛 Big Dad outline-first: {v} ({ {'auto': 'helpers only', 'on': 'helpers + main chat', 'off': 'never'}[v] }). "
              "Applies from the next Read.")
        try:
            verbose = v != "off" and __import__("bd_keep").verbose()
        except Exception:
            verbose = False
        if verbose:
            print("  Note: `keep verbose` is on and wins, so reads stay whole until `bigdad.py keep --remove verbose`.")
        return
    if v != "status":
        print(f"🥛 Big Dad: '{rest[0]}' isn't an outline mode, nothing changed.", file=os.sys.stderr)
    print(f"🥛 Big Dad outline-first: {mode()}. Set: bigdad.py outline auto|on|off (auto = helpers only, where the logs "
          "show it pays).")
