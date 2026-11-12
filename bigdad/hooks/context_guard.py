#!/usr/bin/env python3
"""Big Dad context guard (Claude Code UserPromptSubmit + PostToolUse hook).

Big Dad can't run /compact for you, so this taps you on the shoulder instead: when the
last call in this session carried more than the per-call guide, it shows a one-line note.
The guide is a cost guide (every call re-sends it all), not a documented limit. Default
130k; after `bigdad.py setup` it is min(130k, ~65% of the user's real context window).
It runs on your prompts and after each tool call, so it can warn mid-task too, and inside a
subagent it reads the subagent's own transcript and warns that agent (and notes it for the
parent in agents.jsonl). It warns again only when the load grows another 50%, and starts
fresh after /compact (once a call drops back under the guide). It never blocks anything.

  BIGDAD_GUARD=off        turn it off
  BIGDAD_GUARD_AT=200000  change the guide (tokens per call)
"""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_common as C


def receipt_block(path, sid):
    """The user typed `receipt`: show the full receipt and drop the prompt (no model call, zero tokens)."""
    try:
        import bd_money as M
        text = M.receipt_text(path, sid)
    except Exception:
        text = "🥛 Big Dad: no receipt (this chat's log could not be read)."
    return {"decision": "block", "reason": text}


def last_call_tokens(path):
    return C.last_call(path)[0]


def agent_transcript(event, path, sid, agent, workflows=True):
    """The subagent's own transcript (never the parent's): agent_transcript_path when the event has it,
    else <project>/<session>/subagents/agent-<id>.jsonl next to the main transcript, then a workflow helper's
    <session>/subagents/workflows/<run>/agent-<id>.jsonl (2.0.5: on for every guard, not only the read floor)."""
    path = C.real_path(path) or path
    bases = [os.path.dirname(path), C.dstr(event.get("cwd"))]
    own = C.real_path(event.get("agent_transcript_path"), bases)
    if own and not (path and C.same_file(own, path) and os.path.basename(own).lower() != f"agent-{agent}.jsonl".lower()):
        return own                     # (a path naming the parent's own log is ignored: that's not the agent's size)
    if os.path.basename(path).lower() == f"agent-{agent}.jsonl".lower():
        return path
    d = os.path.join(os.path.dirname(path), sid, "subagents") if path else ""
    want = f"agent-{agent}.jsonl".lower()
    dirs = [d]
    try:                               # workflow agents: <session>/subagents/workflows/<run>/agent-*.jsonl
        wf = os.path.join(d, "workflows")
        dirs += [os.path.join(wf, n) for n in os.listdir(wf)] if workflows else []
    except OSError:
        pass
    for dd in dirs:
        try:                           # case-insensitive by hand: Linux/macOS file names are case-sensitive
            return next(os.path.join(dd, n) for n in os.listdir(dd) if n.lower() == want)
        except (OSError, StopIteration):
            continue
    return ""


def interrupted(agent_path):
    """The agent's last row is the user's '[Request interrupted by user]': it was stopped."""
    lines = [l for l in C.tail_text(agent_path, 6000).splitlines() if l.strip()]
    if not lines:
        return False
    try:
        d = json.loads(lines[-1])
    except ValueError:
        return False
    if not isinstance(d, dict) or d.get("type") != "user":
        return False
    try:
        return "[Request interrupted by user]" in json.dumps(C.dget(d.get("message"), "content", ""))
    except (TypeError, ValueError):
        return False


def done_agents(path):
    """Agents this session is finished with: task notifications in the parent transcript's tail
    (completed/killed/failed) and stop rows in agents.jsonl. Real .meta.json files carry no done flag."""
    done = set()
    for tid, st in re.findall(r"<task-id>(\w+)</task-id>.{0,400}?<status>(\w+)</status>", C.tail_text(path, 1_500_000), re.S):
        if st in ("completed", "killed", "failed"):
            done.add(tid)
    for line in C.tail_text(os.path.join(C.bd_dir(), "agents.jsonl"), 64_000).splitlines():
        if '"stop"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("event") == "stop":
            done.add(C.dstr(d.get("agent_id")))
    return done


def agent_files(path, sid, cut):
    """(path, mtime) of this session's agent logs written since `cut`: <session>/subagents/agent-*.jsonl and the
    workflow agents under <session>/subagents/workflows/<run>/agent-*.jsonl (Claude Code's own layout)."""
    d = os.path.join(os.path.dirname(path), sid, "subagents") if path else ""
    out = []
    dirs = [d]
    try:
        wf = os.path.join(d, "workflows")
        dirs += [os.path.join(wf, n) for n in os.listdir(wf) if os.path.getmtime(os.path.join(wf, n)) >= cut]
    except OSError:
        pass
    for dd in dirs:
        try:
            names = os.listdir(dd)
        except OSError:
            continue
        for n in names:
            if not (n.startswith("agent-") and n.endswith(".jsonl")):
                continue
            p = os.path.join(dd, n)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m >= cut:
                out.append((p, m))
    return out


def running_agents(path, sid, cut_s=600):
    """Subagents of this session that look alive: {id: (tokens of their last call, type, mtime)}. Only logs written
    in the last cut_s seconds; drops agents stopped by the user (interrupted marker at the end of their log) and
    agents Claude Code marked finished (.meta.json "finished": true). Last-call sizes are cached by mtime and size
    per session: an agent log that hasn't changed since the last scan costs one stat, and one that grew is read
    only past the size seen last time (1.7.0: a live agent cost a 200 KB tail parse on every main-chat call)."""
    out = {}
    cache_path = os.path.join(C.state_dir(), f"bigdad-agentscan-{sid}.json")
    cache, fresh = load_state(cache_path), {}
    for p, m in agent_files(path, sid, time.time() - cut_s):
        hit = cache.get(p)               # v1.6: an agent log whose mtime hasn't moved isn't re-parsed
        hit = hit if isinstance(hit, list) and len(hit) in (3, 4) else None
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if hit and hit[0] == m:
            tokens, stopped = hit[1], hit[2]
        else:
            grown = (C.last_call_since(p, hit[3]) if hit and len(hit) == 4 and isinstance(hit[1], int)
                     and isinstance(hit[3], int) and size > hit[3] else None)   # same size, new mtime: rewritten
            if grown is None:
                tokens, _ = C.last_call(p)
            else:
                tokens = grown[0] or hit[1]   # no new call in the appended bytes: the last one still stands
            stopped = bool(tokens) and interrupted(p)
        fresh[p] = [m, tokens, stopped, size]
        if not tokens or stopped:
            continue
        meta = C.load_json(p[:-6] + ".meta.json")
        if meta.get("finished") is True:
            continue
        out[os.path.basename(p)[6:-6]] = (tokens, C.dstr(meta.get("agentType")), m)
    if fresh != cache:
        save_state(cache_path, fresh)
    return out


def drop_done(path, agents):
    """Remove agents this session is done with (task notifications, stop rows); reads the parent's tail, so only
    called when there is something to say."""
    if agents:
        done = done_agents(path)
        for aid in list(agents):
            if any(aid == x or aid.startswith(x) or x.startswith(aid) for x in done if x):
                del agents[aid]
    return agents


def agents_over(path, sid, limit, live=None):
    """Subagents of this session whose own last call passed the guide: {id: (tokens, type)}.
    Reads <session>/subagents/agent-*.jsonl next to the main transcript, only files written in the
    last 10 minutes, then drops agents that are done: stopped by the user (interrupted marker at the
    end of their transcript), reported done in the parent's task notifications, or stopped in agents.jsonl."""
    live = running_agents(path, sid) if live is None else live
    cut = time.time() - 600
    out = {a: (n, kind) for a, (n, kind, m) in live.items() if n >= limit and m >= cut}
    return drop_done(path, out)


def load_state(state):
    try:
        d = json.load(open(state))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, d):
    try:
        with open(state, "w") as f:
            json.dump(d, f)
    except OSError:
        pass


def soft_note(sid, agent, event, tokens, cap):
    """One heads-up per agent at ~80% of the hard-stop cap: finish this step, get the handoff ready."""
    if not (C.SOFT_AT * cap <= tokens < cap):
        return None
    mark = os.path.join(C.state_dir(), f"bigdad-soft-{sid}-{agent}")
    if os.path.exists(mark):
        return None
    try:
        open(mark, "w").close()
    except OSError:
        return None
    return (f"🥛 Big Dad 🟡 ~{tokens // 1000:,}k of this agent's {C.stop_label(cap)} per call: finish this step, get the "
            f"handoff ready ({C.handoff_path(C.dstr(event.get('agent_type')), agent)}: goal, done, files + state, next "
            "step, risks).")


WAYS = ("Ways out: /compact now; set autoCompactWindow (settings or /autocompact; restart open sessions for it to apply) "
        "so it compacts earlier; fewer forks (each one carries this whole chat).")


def main(event=None, standalone=True):
    if C.off("BIGDAD_GUARD"):
        return
    event = C.read_event() if event is None else event
    if event is None:
        return
    agent = C.dstr(event.get("agent_id"))
    hook = C.dstr(event.get("hook_event_name")) or "UserPromptSubmit"
    limit = C.guard_at()
    sid = C.dstr(event.get("session_id")) or "x"
    path = C.dstr(event.get("transcript_path"))
    if agent:
        path = agent_transcript(event, path, sid, agent)
        if not path:
            return                     # can't find the agent's own transcript: say nothing rather than guess
    if hook == "UserPromptSubmit" and not agent and C.dstr(event.get("prompt")).strip().lower() in (
            "receipt", "🧾", "bigdad receipt", "big dad receipt"):
        return C.emit(receipt_block(path, sid), standalone)
    tokens = last_call_tokens(path)
    state = os.path.join(C.state_dir(), f"bigdad-guard-{sid}-{agent or 'main'}.json")
    if hook == "PreCompact":           # 2.0.5: note the size a compact starts from, so one that starts under the
        if not agent and tokens:       # guide still gets its receipt (live: 116,946 -> 71,335, no warning, no receipt)
            with C.lock(state):
                if not os.path.exists(state):
                    save_state(state, {"peak": tokens, "pre": 1})
        return None
    own = None                         # this thread's own warning, if due
    receipt = None
    learn = C.learning()               # learning first (2.0.6): calls past the guide counted, no note, no latch
    if tokens < limit:
        st = load_state(state) if tokens and os.path.exists(state) else {}
        if st.get("pre") and tokens == st.get("peak"):
            pass                       # this hook ran before the compact landed: same size is not a verdict yet
        elif tokens and os.path.exists(state):   # back under the limit (after /compact): receipt, then start fresh
            peak = st.get("peak") or st.get("warned_at") or 0
            if not agent and isinstance(peak, (int, float)) and peak and tokens <= 0.7 * peak:
                receipt = (int(peak), tokens)
                C.append_jsonl(os.path.join(C.bd_dir(), "receipts.jsonl"), {
                    "ts": C.now(), "session": sid[:8], "before": int(peak), "after": tokens, "saved_per_call": int(peak) - tokens})
            try:
                os.remove(state)
            except OSError:
                pass
        elif not tokens and os.path.exists(state):   # 0 = a compact since the last call (bd_common.last_call): the
            with C.lock(state):                         # old warning's 1.5x latch is about a size that is gone; keep
                st = load_state(state)                  # the peak for the compact receipt (Grok review, round 4)
                if st.pop("warned_at", None) is not None:
                    save_state(state, st)
    elif learn:
        if hook == "PostToolUse":
            C.learn_seen("over_guide")
    else:
        with C.lock(state):            # two copies of the hook: still one warning per 1.5x
            st = load_state(state)
            warned = st.get("warned_at", 0)
            peak = max(tokens, st.get("peak") or 0) if isinstance(st.get("peak") or 0, (int, float)) else tokens
            if not (warned and tokens < warned * 1.5):
                st["warned_at"] = tokens
                own = tokens
            if peak != st.get("peak") or own:
                st["peak"] = peak
                save_state(state, st)
    k = f"{(own or 0) // 1000:,}k"
    if agent:
        cap = C.stop_cap() if tokens else 0
        soft = soft_note(sid, agent, event, tokens, cap) if cap else None
        if own is not None and cap and tokens >= cap:
            own = None                 # past the cap the hard stop's deny reason says it (with the handoff path)
        # learning first (2.0.6): the soft heads-up stays. It exists only with the hard stop on (the user's own opt-in,
        # which acts while learning), and `agent-stop` promises it; holding it cut agents off with no warning.
        if own is None:                # said once; again only when the load grows another 50% (1.5x -> 2.25x -> 3.4x).
            #                            v1.4 said "stop now" on every call past 1.5x: four identical notes at 26x..31x
            #                            changed nothing and each one grew the context it complained about.
            return C.emit({"hookSpecificOutput": {"hookEventName": hook, "additionalContext": soft}}, standalone) if soft else None
        C.append_jsonl(os.path.join(C.bd_dir(), "agents.jsonl"), {
            "ts": C.now(), "event": "over-guard", "session": sid[:8], "agent_id": agent[:17],
            "agent_type": C.dstr(event.get("agent_type")), "tokens": tokens, "guard_at": limit})
        # with the hard stop on, the handoff goes to a named file the relaunch line points at; with it off (the default)
        # there is no file and no relaunch line, so the report itself is the handoff (bench round 4: a file with
        # nowhere to go was ignored, then explained in the report and again in the parent's answer)
        if cap:
            hand = (f"write your handoff file to {C.handoff_path(C.dstr(event.get('agent_type')), agent)} "
                    "(what's done, what's left, how to verify) and report back")
        else:
            hand = "report back now with what's done, what's left and how to verify (that report is the handoff)"
        if tokens >= 1.5 * limit:
            text = (f"🥛 Big Dad 🔴 stop now. This agent re-sends ~{tokens // 1000:,}k tokens per call, "
                    f"{tokens / limit:.1f}x the {limit // 1000}k cost guide. Don't start new work: {hand}, so the "
                    "parent can relaunch you fresh.")
        else:
            text = (f"🥛 Big Dad 🟡 guard: this agent re-sends ~{k} tokens per call, over the {limit // 1000}k cost guide "
                    f"(auto-compact may not cover a subagent). Finish this step, then {hand}, so the parent can "
                    "relaunch you fresh. Until then: no re-reading files, no long outputs.")
        return C.emit({"hookSpecificOutput": {"hookEventName": hook, "additionalContext": text + (" " + soft if soft else "")}},
                      standalone)
    # main thread: its own size, plus any subagent of this session that passed the guide (once per agent per 1.5x)
    astate = os.path.join(C.state_dir(), f"bigdad-guard-{sid}-agents.json")
    pending = C.pop_pending(sid)       # a capped agent's relaunch line, queued by the agent monitor
    live = running_agents(path, sid) if not learn else {}
    over = {} if not live else {
        a: (n, kind) for a, (n, kind, m) in live.items() if n >= limit and m >= time.time() - 600}
    due = {}
    if over:
        with C.lock(astate):
            seen = load_state(astate)
            for aid, (n, kind) in over.items():
                w = seen.get(aid, 0)
                if not (isinstance(w, (int, float)) and w and n < w * 1.5):
                    due[aid] = (n, kind)
            due = drop_done(path, due) if due else due  # the parent's 1.5 MB tail is read only when a note is actually due
            for aid, (n, _) in due.items():
                seen[aid] = n
            if due:
                save_state(astate, seen)
    pause = hnote = None
    if hook == "PostToolUse" and tokens >= limit:
        kind, text = pause_due(state, tokens, limit, path) or (None, None)
        if kind == "pause":
            pause = text
        elif kind == "note":
            hnote = text               # headless: a note per 2x step instead of a pause
    note = reload_note(hook, sid, path) if not learn else None
    redirect = redirect_switch(event, hook, sid, path, tokens, limit) if not agent and not learn else ""
    side = side_question(event, hook, sid, tokens, limit) if not agent and not learn else ""
    if learn:
        receipt = None                 # the compact's receipts.jsonl row is still written; no note while learning
    if own is None and not due and not receipt and not pause and not hnote and not note and not redirect \
            and not pending and not side:
        return
    msgs, ctx = [], []
    for text in pending:
        msgs.append(text)
        ctx.append("🥛 Big Dad " + text + " Tell the user in one 🥛 line; relaunch only if the work is unfinished.")
    if hnote:
        msgs.append(hnote)
        ctx.append(f"🥛 Big Dad 🛑 headless: this run re-sends ~{tokens // 1000:,}k tokens per call, {tokens / limit:.1f}x the "
                   f"{limit // 1000}k guide. Finish this step, then /compact or hand off and restart; no re-reads, short outputs.")
    if side:
        if side == "tip":
            msgs.append(f"💬 side question in a {C.money(tokens)} chat: `/btw <question>` answers it without adding "
                        "it to this chat (native Claude Code).")
        ctx.append("🥛 Big Dad: side question, outside the current goal. Answer it briefly from what you already have; "
                   "don't read files or run tools for it unless the user asks. Then carry on with the goal.")
    if redirect and not strong(redirect):
        msgs.append(f"🔁 \"{redirect}\" noted: if the old plan is over, Claude closes what it opened for it.")
        ctx.append(f"🥛 Big Dad: the user said \"{redirect}\". If that ends the old plan, close previews, players and "
                   "background jobs you started for it and stop re-sending its drafts. If it is unclear, ask in one line.")
    elif redirect:
        msgs.append(f"🔁 plan dropped (\"{redirect}\")? Claude will close what it opened for the old plan; "
                    "say yes to a /compact that drops it.")
        ctx.append(f"🥛 Big Dad: the user dropped the old plan (\"{redirect}\"). Close previews, players and background "
                   "jobs you started for it; delete or mark superseded drafts/screens; don't keep re-sending them. Offer a "
                   "/compact that drops the old plan (Big Dad's slim compact will drop it and keep the new direction), on a yes.")
    if receipt:
        b, a = receipt
        msgs.append(f"🧾 receipt: this chat went from {C.money(b)} to {C.money(a)} tokens per call (-{(b - a) // 1000:,}k per "
                    f"call, -{100 * (b - a) // b}%). Every later call re-sends that much less: about {20 * (b - a) / 1e6:.1f}M "
                    "fewer input tokens over the next 20 calls (estimate).")
        try:                           # the full receipt: tokens and what each compact saved
            import bd_money as M
            msgs.append("\n" + M.receipt_text(path, sid))
        except Exception:
            pass
    if note:
        msgs.append(note)
    if own is not None:
        # the long WAYS text goes to the user only (systemMessage costs no tokens); the agent gets one short line
        if hook == "PostToolUse":
            tip = "Finish this step without re-reading files or long outputs, then suggest /compact or a fresh session."
        else:
            tip = "No re-reads, short outputs; suggest /compact or a fresh session at a natural break."
        dot = "🔴" if tokens >= 1.5 * limit else "🟡"
        msgs.append(f"{dot} every call in this session re-sends {C.money(tokens)} tokens (cost guide {limit // 1000}k, not a limit). {WAYS}")
        ctx.append(f"🥛 Big Dad {dot} guard: this session re-sends ~{k} tokens per call, over the {limit // 1000}k cost guide. {tip}")
    for aid, (n, kind) in due.items():
        who = f"agent {aid[:8]}" + (f" ({kind})" if kind else "")
        msgs.append(f"🟡 {who} re-sends {C.money(n)} tokens per call, over the {limit // 1000}k guide; auto-compact may not cover it.")
        told = os.path.exists(os.path.join(C.state_dir(), f"bigdad-guard-{sid}-{aid}.json"))   # its own guard fired
        ctx.append(f"🥛 Big Dad 🟡 guard: {who} is at ~{n // 1000:,}k tokens per call, over the {limit // 1000}k cost guide. "
                   + (("It has been told to finish its step, write its handoff file and report back; when it does, "
                       if C.stop_cap() else
                       "It has been told to finish its step and report back with what's done and what's left; when it does, ")
                      if told else "It may still be running (its own warning has not fired yet); when it reports back, ")
                   + "relaunch it fresh from a short handoff instead of continuing it (an agent's context never shrinks on its own).")
    out = {"systemMessage": "🥛 Big Dad: " + " ".join(msgs)} if msgs else {}
    if ctx:
        out["hookSpecificOutput"] = {"hookEventName": hook, "additionalContext": " ".join(ctx)}
    if pause:
        out["continue"] = False
        out["stopReason"] = pause
    return C.emit(out or None, standalone)


def entrypoint(path):
    """How this session runs (cli = interactive; sdk-cli etc. = headless), from the newest row that says."""
    found = re.findall(r'"entrypoint":\s*"([^"]+)"', C.tail_text(path, 60_000)) if path else []
    return found[-1] if found else ""


def headless(entry):
    """True for a run nobody is watching (claude -p, the Agent SDK): entrypoint sdk-* or headless. Everything else
    (cli, desktop, IDE, an entrypoint string we have not seen yet, or none logged) is treated as interactive, so a new
    surface never silently loses the brake (desktop would have)."""
    e = entry.lower()
    return e.startswith("sdk") or e == "headless"


def pause_due(state, tokens, limit, path):
    """Past the pause point (off by default; `bigdad.py pause 2` sets 2x the guide) in the main chat: ("pause", text) once per 1.5x step in an
    interactive session; headless, where a pause would end the run, ("note", text) once per 2x step instead of
    silence, unless BIGDAD_PAUSE_HEADLESS=1 asks for the real pause. None when nothing is due."""
    factor = C.pause_at()
    if not factor or tokens < factor * limit:
        return None
    note = headless(entrypoint(path)) and os.environ.get("BIGDAD_PAUSE_HEADLESS") != "1"
    key, step = ("noted_at", 2.0) if note else ("paused_at", 1.5)
    with C.lock(state):
        st = load_state(state)
        last = st.get(key) or 0
        if isinstance(last, (int, float)) and last and tokens < last * step:
            return None
        st[key] = tokens
        save_state(state, st)
    if note:
        return ("note", f"🛑 headless: this run re-sends {C.money(tokens)} tokens per call ({tokens / limit:.1f}x the "
                        f"{limit // 1000}k guide) and Big Dad does not pause a headless run (BIGDAD_PAUSE_HEADLESS=1 makes it). "
                        "Realign: /compact, or restart the run from a short handoff.")
    return ("pause", f"🥛 Big Dad 🛑 paused this turn: this chat re-sends {C.money(tokens)} tokens per call ({tokens / limit:.1f}x the "
                     f"{limit // 1000}k guide), and every call from here sends it again. Realign: /compact (Big Dad adds its slim "
                     "instructions; yours go first), or a fresh session from a short handoff. Big Dad shows a receipt after the "
                     "compact. To keep going as is, just say continue.")


MACHINE = ("<task-notification", "<agent-message", "another claude session sent a message", "<system-reminder",
           "<local-command", "<command-name", "<bash-input", "<bash-stdout")


def machine_prompt(prompt):
    """Claude Code feeds some text in as a 'prompt' that the user never typed (seen headless 2026-11-08: a subagent's
    hand-back arrived as 'Another Claude session sent a message: <agent-message ...>')."""
    head = prompt.lstrip()[:200].lower()
    return any(head.startswith(m) or m in head[:120] for m in MACHINE)


# v1.6: the word-overlap "new topic?" guess (topic_switch) is gone: thresholds came from one chat and it re-sent
# text on ordinary prose. redirect_switch (the user's own words: "scrap this", "start over") stays.


# 1.7.0: only phrases that end a plan arm the slim compact's drop. Softer ones get a note and drop nothing. Loose
# ones ("go back to", "new plan", "undo that", "redo it", "I don't like it") are gone: they often keep the plan.
STRONG_WORDS = r"scrap (?:this|that|it)|start (?:over|again)|from scratch|throw (?:it|that) out"
SOFT_WORDS = r"never ?mind|change of plans?|forget (?:that|this)"
STRONG = re.compile(r"(?:" + STRONG_WORDS + r")", re.I)
REDIRECT = re.compile(r"\b(?:" + STRONG_WORDS + "|" + SOFT_WORDS + r")\b", re.I)
# a negation up to three words before the phrase ("don't start over", "no need to redo it", "I'm not going to scrap this")
NEGATED = re.compile(r"\b(?:don['’]?t|dont|do not|never|not|no (?:need|reason|point)(?: in| to)?|won['’]?t|"
                     r"shouldn['’]?t|wouldn['’]?t|can['’]?t|cannot|without|unless|if|instead of|rather than)"
                     r" *(?:\w+ +){0,3}$", re.I)
REDIRECT_EVERY = 8                     # user turns between two redirect notes


def redirect_phrase(prompt):
    """The first redirect phrase the user typed ('scrap this', 'start over', ...) that is not negated, else ''.
    Word boundaries, any case, any prompt length. Punctuation between a negation and the phrase ends the negation
    ("it's not working, start over" is a redirect; "don't start over" is not)."""
    text = " ".join(C.dstr(prompt).split())
    for m in REDIRECT.finditer(text):
        if NEGATED.search(text[max(0, m.start() - 80):m.start()]):
            continue
        return m.group(0)
    return ""


def strong(phrase):
    """True for a phrase that ends a plan ("scrap this", "start over"): only these arm the slim compact's drop."""
    return bool(phrase and STRONG.fullmatch(phrase))


def redirect_switch(event, hook, sid, path, tokens, limit):
    """'scrap this, start over' in a heavy chat: one short note at any prompt length.
    Returns the redirect phrase when the user dropped the plan, else ''. Counts user turns for the cooldown (one
    note per REDIRECT_EVERY turns). Only a strong phrase leaves the dropped plan in the redirect flag for the next
    slim compact; a soft one ("never mind") is a note only.
    Skips: machine-fed prompts, negations, light chats (under the guide), BIGDAD_REDIRECT=0."""
    if hook != "UserPromptSubmit" or C.off("BIGDAD_REDIRECT"):
        return ""
    prompt = C.dstr(event.get("prompt"))
    if not prompt.strip() or machine_prompt(prompt):
        return ""
    phrase = redirect_phrase(prompt)
    flag = C.redirect_file(sid)
    with C.lock(flag):
        st = load_state(flag)
        turn = (st.get("turn") if isinstance(st.get("turn"), int) else 0) + 1
        st["turn"] = turn
        C.settle_dropped(st, path)      # the compact that was told to drop it has finished
        fired = st.get("fired_at") if isinstance(st.get("fired_at"), int) else 0
        if not phrase or tokens < limit or (fired and turn - fired < REDIRECT_EVERY):
            save_state(flag, st)
            return ""
        st["fired_at"] = turn
        if strong(phrase):
            st["dropped"] = {"ts": C.now(), "phrase": phrase, "prompt": " ".join(prompt.split())[:160], "tokens": tokens}
        save_state(flag, st)
    return phrase


# 2.0.3: a side question in a heavy chat ("btw ...", "quick question ..."). Only at the start of the prompt: the same
# words mid-sentence are usually part of the work. Claude Code's native /btw answers one without adding it to the chat.
SIDE = re.compile(r"^\W*(?:btw|by the way|quick question|side question|unrelated(?: question)?|off[- ]topic|"
                  r"random question|different question|not related)\b", re.I)
SIDE_AT = 0.5                          # x the guide: below that a side question costs little


def side_question(event, hook, sid, tokens, limit):
    """'' / 'note' (Claude: answer briefly, no file reads) / 'tip' (also tell the user about /btw, once per session).
    Skips machine-fed prompts, light chats, BIGDAD_SIDE=off."""
    if hook != "UserPromptSubmit" or C.off("BIGDAD_SIDE") or tokens < SIDE_AT * limit:
        return ""
    prompt = C.dstr(event.get("prompt"))
    if not SIDE.search(prompt) or machine_prompt(prompt):
        return ""
    flag = C.redirect_file(sid)
    with C.lock(flag):
        st = load_state(flag)
        if st.get("side_tip"):
            return "note"
        st["side_tip"] = C.now()
        save_state(flag, st)
    return "tip"


def reload_note(hook, sid, path):
    """Once per session: the per-tool-call guard isn't running here (the session started before Big Dad's hooks
    were installed or updated), so only the user's messages get checked. Fix: /reload-plugins."""
    if hook != "UserPromptSubmit" or not path:
        return None
    flag = os.path.join(C.state_dir(), f"bigdad-reload-{sid}.json")
    if os.path.exists(flag) or not C.hooks_off(path, sid):
        return None
    try:
        open(flag, "w").close()
    except OSError:
        pass
    return ("⚠️ Big Dad's per-tool-call guard isn't running in this session (it started before Big Dad's hooks were "
            "installed or updated), so it only checks when you send a message. Run /reload-plugins to turn it on.")


if __name__ == "__main__":
    main()
