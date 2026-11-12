---
name: bigdad
description: 'Token watchdog: what each call re-sends, re-read and agent guards, proven cuts. Use for "big dad" or limits.'
---

# Big Dad

Order is fixed: **observe → note → propose → act only with the user's yes → prove in the mode it affects → measure
again**. Measured numbers only; label each **verified** (logs or a live run), **estimated** (say how) or **not
measured**. Big Dad never changes the user's settings, CLAUDE.md, AGENTS.md or memory on its own: propose the edit.

**Native first:** `autoCompactWindow` (settings, `/autocompact`, subagent frontmatter), `/compact <instructions>`,
`/context`, `/usage`, `ENABLE_TOOL_SEARCH`, `skillOverrides`, `permissions.deny`, PostCompact hooks. Big Dad fills
the gaps: the warning nobody gives, the per-agent size, the measurement, the proof.

## Commands: `sh <this skill's folder>/scripts/bigdad.sh <cmd>`

Same command on Windows, macOS and Linux: it finds Python 3 itself. Where output says `bigdad.py <cmd>`,
run it this way.

`fresh` (run first when the last check is days old) · `settle` · `self [DAYS]` (Big Dad's own cost vs saved) · `all 10` ·
`agents` · `health` · `report` · `receipt` · `keep x ["why"]` · `shrink` · `tidy` · `setup` · `notes` / `watch` /
`note "..."`. All of these only read, except `setup`, `note`, `watch`, `keep` (Big Dad's own state). On a yes
only: `agent-stop 100k`, `pause 2`, `sentinel on`, `statusline on`, `tidy yes`, `settle reset`, and `probe`, `proof`,
`compare` (live runs that cost money).
Full list, surfaces table, cut table, measured `autoCompactWindow` results, agent hard stop and the watch loop:
`references/detail.md` (Read it only when the task needs that part). Save output before a cut (`> file`, `--redact`
if shared): that is the baseline.

## Rules

- **Report:** one line per finding, biggest first: `what -> measured cost -> fix`. Lead with re-reads and the mode
  and model that hold the most. Count, don't list.
- **Receipt:** the user types `receipt` (or 🧾); the hook shows it at 0 tokens. Quote its numbers; never estimate a
  price or % it does not show.
- **Cuts:** one yes each, back up first, native lever first, every cut reversible. Never delete a skill, plugin or
  file; never cut what the user uses; a `keep` item is "your choice", and a "no" is `keep <x> --declined`.
- **Prove** each cut in the mode it affects (`probe`, `proof read`, `tools`, `compare`), then measure again.
- **Settle:** run `settle` before an audit. A settled area is at its floor: skip it; audit only areas still moving
  or grown back. A proven cut is done: don't propose it again. Settled areas get a drift check every 21 days.
- **Learning first:** with no baseline yet, the hooks only count (no blocks, cuts or notes) until the baseline covers
  30 calls; `baseline` shows the counts. Report them as observed, never as saved. `BIGDAD_LEARN=off` acts at once.
- **One goal:** hold the end goal in the user's newest words. A prompt that changes direction: say in one line
  which earlier reads and drafts no longer serve the goal, and offer a slim `/compact` (on a yes). A side question
  ("btw ..."): answer briefly, no file reads; `/btw` keeps one out of the chat.
- **Agents:** set `model` on every Agent call (cheapest tier for lookups). A fork re-sends the whole chat: prefer a
  fresh agent with a short brief (paths, not pasted text). For a heavy agent offer `autoCompactWindow` in its
  frontmatter first (restart open sessions to apply); `agent-stop` is the fallback. An agent over the guide finishes
  its step, reports back (the report is the handoff), and is relaunched fresh. Ask for short reports.
- **Keep it hot:** Grep first, then Read only the lines needed; don't re-read an unchanged file still in context.
  Trim command output (`| tail -40`). A shrunk Bash output ends with a "[🥛 Big Dad shrink: ...]" note naming the
  full output's file: Grep or Read that file with offset/limit; don't re-run the command. When the guard says a
  call is over the guide, suggest `/compact` or a fresh session at the next natural break.
- **Plan ended** ("scrap this", "start over", "from scratch", "throw it out"): say so in one line, close the
  previews, players and jobs opened for the old plan, mark its drafts superseded, and offer a `/compact` that drops
  it (compact only on a yes). Softer ("never mind", "forget that"): a note only.
- Only `/compact`, `/clear` or a fresh chat or agent shrinks context; every call re-sends the whole chat ("cache
  read" is that re-send). Images cost ~width*height/750 tokens.

**Footer:** **Cut:** what was cut and how it was proven (mode + command). **Tokens:** before → after per mode, or
"not measured yet". Never give a percentage that wasn't measured.
