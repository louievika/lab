# Big Dad: detail (open only when the task needs it)

Moved out of SKILL.md (in 2.0.2, and the rest in 3.0.1) so the skill page costs less on every call after `/bigdad`:
the page rides on every later call and is re-attached after each compact. Nothing here is a new rule. The page names
which heading to Read when: Audit, Cut table, Agents, Keep it hot, Redirects, Commands.

## Audit

- **Native first:** `autoCompactWindow` (settings, `/autocompact`, subagent frontmatter), `/compact <instructions>`,
  `/context`, `/usage`, `ENABLE_TOOL_SEARCH`, `skillOverrides`, `permissions.deny`, PostCompact hooks. Big Dad fills
  the gaps: the warning nobody gives, the per-agent size, the measurement, the proof.
- **Settle:** run `settle` before an audit. A settled area is at its floor: skip it; audit only areas still moving
  or grown back. A proven cut is done: don't propose it again. Settled areas get a drift check every 21 days.
- **Learning first:** with no baseline yet, the hooks only count (no blocks, cuts or notes) until the baseline covers
  30 calls; `baseline` shows the counts. Report them as observed, never as saved. `BIGDAD_LEARN=off` acts at once.
- **Report:** one line per finding, biggest first: `what -> measured cost -> fix`. Lead with re-reads and the mode
  and model that hold the most. Count, don't list. The receipt can also be typed as 🧾.
- **Footer:** **Cut:** what was cut and how it was proven (mode + command). **Tokens:** before -> after per mode, or
  "not measured yet". Never give a percentage that wasn't measured.

## Redirects

- **One goal:** hold the end goal in the user's newest words. New direction or "btw": follow the hook's note when
  one comes, and the lines below either way (the hook's redirect note fires only in a heavy chat).
- **New direction:** say in one line which earlier reads and drafts no longer serve the goal, and offer a slim
  `/compact` (on a yes).
- **Side question** ("btw ..."): answer briefly, no file reads; `/btw` keeps one out of the chat.
- **Plan ended** ("scrap this", "start over", "from scratch", "throw it out"): say so in one line, close the
  previews, players and jobs opened for the old plan, mark its drafts superseded, and offer a `/compact` that drops
  it (compact only on a yes). Softer ("never mind", "forget that"): a note only.

## Commands

Every command, one line each. Run each as `sh <this skill's folder>/scripts/bigdad.sh <cmd>`: the same command on
every OS, it finds Python 3 itself.

    fresh        today's date, installed vs latest Claude Code, changelog lines since last check, setup diff. Run FIRST when the profile or last check is older than a few days.
    settle       each area (startup load, re-sent above guide, chats past guide): settled at its floor, moving, or grown back. Settled = two baselines in a row within 5%; then only a drift check every 21 days. `settle reset` forgets the floors
    self [DAYS]  Big Dad's own cost vs what it saved, per part, from the logs (default 14 days), split into building/testing Big Dad vs everyday work; proposes switching off a part that loses
    agent-stop 100k|2|off   opt-in hard stop for subagents (only with the user's yes): 100k = tokens per call; 1-20 = x the guide; >=1000 or k/m = tokens
    pause 2|off        main chat: end the turn past 2x the guide, once per 1.5x step (OFF by default, opt-in on a yes; every interactive surface; headless gets one note per 2x step instead)
    helper-floor auto|off|4k   helpers: a small re-read goes through (auto = under call size / calls so far); main chat unchanged
    sentinel on|off|check   opt-in user-level SessionStart hook that says so when the plugin can't load (folder gone, hooks not running, no Python); only on a yes, backs up settings.json first
    statusline on|off [--replace]   opt-in status line (sent · last compact · plan limits); keeps an existing one unless --replace; off puts it back
    shrink [on|off]    output shrink status + tokens cut by command type (on by default; Bash/PowerShell, and Grep results over 4,000 chars: BIGDAD_GREP_SHRINK=off for Grep only)
    keep x ["why"] | --list | --remove x | verbose   what the user chose on purpose: shown as "your choice", never a cut. When the user says no to a proposal, run `keep <x> --declined`
    tidy [yes|undo]    Big Dad's own leftovers (state files, its test runs) to its trash; dry run first, moves on a yes
    setup        observe this machine (OS, surfaces, models, real window, skills/plugins/MCP, every plugin's hooks with launch cost) -> ~/.claude/bigdad/profile.json. Reports run it themselves when it is missing, >7 days old, or the Claude Code version changed.
    all 10       re-reads, startup load, usage, skills, models x modes (24 h), files
    agents       every subagent seen: running/done, model, size now, total, flags
    health       security + upkeep + hooks per tool call + privacy before publishing
    report       one local HTML page (tokens per call, startup load, models x modes, agents, health, profile); no server, no network
    notes        what the last watched sessions noted;  watch  start noting;  note "..."  add one
    modes | models 8 | tools | probe | proof read | compare <old> <new>   deeper measurement and A/B proofs

## What works where

| Surface | measure (logs) | hooks (guards, slim compact, agent monitor) | cuts | report |
|---|---|---|---|---|
| CLI, headless `claude -p`, SDK | yes | yes | yes | yes |
| Desktop app, IDE extensions | expected (same logs); being verified | expected (same hooks); being verified | yes | yes |
| Cloud / remote agents, claude.ai chat, phone | no local logs: not measured | no | settings-side only | no |

Every report line is labelled by entrypoint (CLI, SDK/headless, Desktop, IDE...). Say "not measured" for anything
from a surface without local logs.

## Cut table

One yes each, back up first, native lever first, every cut reversible. Never delete a skill, plugin or file; never
cut what the user uses; a `keep` item is "your choice", and a "no" is `keep <x> --declined`. Save output before a
cut (`> file`, `--redact` if shared): that is the baseline. Prove each cut in the mode it affects (see Prove below),
then measure again.

| What | Native lever first | Fallback | Check first |
|---|---|---|---|
| skill you own, never used | `disable-model-invocation: true` in its SKILL.md, or `skillOverrides` | move to `~/.claude/skills-off/` | "never used" in `skills` |
| plugin skill | (skillOverrides doesn't reach these) | `enabledPlugins["name@source"]: false` | the plugin's used skills and hooks |
| MCP server / connector | connector toggle; `disabledMcpServers`; tool search keeps names only | `/mcp` | its call count in `skills` |
| built-in tool | `permissions.deny` in settings | | the user doesn't need it |
| heavy sessions | `autoCompactWindow` (settings or `/autocompact`) | `/compact` at a break | guard notes in the session |
| heavy subagents | `autoCompactWindow` in the subagent's frontmatter (100k-1M); relaunch fresh from a handoff | shorter briefs, haiku for lookups | `agents` |
| CLAUDE.md / AGENTS.md | trim; move rarely needed parts into a skill | | size and duplicates in `health` |

No skill cut is suggested on under 30 days of logs. Before suggesting a fix or building one, check `skills` for the
user's own skills that already do that job (terse replies, usage stats, memory, handoffs, audits) and use those first.

## autoCompactWindow, measured

When offering it, say: **restart open sessions for it to apply**. A new session with the setting at 100k
auto-compacted a subagent 5 times and never passed 79k per call; a session already open when the setting was added
let a subagent reach 258k per call. `setup` and `agents` flag open sessions that predate the setting. Once in a
subagent (frontmatter `autoCompactWindow: 100000`, haiku): native compact fired at 111,726 -> 63,461, but the next
call still sent 142,519. So `autoCompactWindow` first; `agent-stop 100k` is the fallback.

## Prove, in the mode the cut affects

- `probe <flags>`: headless A/B of a startup cut.  `proof read`: read guard A/B, learning set aside (`BIGDAD_LEARN=off`).
- `tools`: newest logs per mode with the first call and the tools carried; run before and after.
- `compare <old> <new>`: first calls of two real sessions.

Settings changes reach new sessions and new agents; the current chat keeps its old start. Then `all` and `models`
after a few new sessions, next to the baseline; say what isn't measured yet.

## Agents

Set `model` on every Agent call (cheapest tier for lookups). A fork re-sends the whole chat: prefer a fresh agent
with a short brief (paths, not pasted text). For a heavy agent offer `autoCompactWindow` in its frontmatter first
(restart open sessions to apply); `agent-stop` is the fallback. An agent told at 85% of the guide finishes its step,
reports back under the guide (the report is the handoff), and is relaunched fresh. Ask for short reports.

## Agent guard and hard stop

- One big-model background agent at a time (`BIGDAD_MAX_BIG_AGENTS`, default 1; "big" = the priciest tier seen in
  this user's logs, override `BIGDAD_BIG_MODEL`; tiers haiku < sonnet < opus, any other family counts as the top
  tier, `BIGDAD_MODEL_TIERS` sets the order cheapest first).
- A warning is advice, not a brake (measured: two real helpers kept going to 183k and 196k). Past 1.5x the guide the
  agent is told to stop now, again each time its load grows another 50%; the same call 4+ times with no edit in
  between gets a note.
- Hard stop (OFF by default, unmeasured in tokens): at 80% of the cap the agent is told to get its handoff ready
  (when the 85% guard note told it first, this one waits for the first call at or past the guide).
  Past the cap it can only Write/Edit its handoff (under `~/.claude/bigdad/handoffs/`) and report back. Never the
  main chat. When a capped agent stops, the main chat gets one relaunch line. `agent-stop 25` is off: numbers 1-20
  are x the guide, so `500` can't mean 500x.
- The stop gate costs ~13 ms median (max ~23 ms) per tool call when off (one sh start, no Python).

## Keep it hot

- Grep first, then Read only the lines needed; don't re-read an unchanged file still in context. Trim command
  output (`| tail -40`).
- A shrunk Bash or Grep output ends with a "[🥛 Big Dad shrink: ...]" note naming the full output's file: Grep or
  Read that file with offset/limit; don't re-run the command.
- When the guard says a call is over the guide, suggest `/compact` or a fresh session at the next natural break.
- Only `/compact`, `/clear` or a fresh chat or agent shrinks context; every call re-sends the whole chat ("cache
  read" is that re-send). Images cost ~width*height/750 tokens.

## Context guide

The context guard notes when a call re-sends more than the per-call guide (130k, or ~65% of the real window after
`setup`; a cost guide, not a limit; unmeasured). Inside a subagent it warns that agent once at 85% of its own guide, so its finish and report land under the guide (with
`agent-stop` on, the 80%-of-cap note counts as that one when it comes first), and the main chat once the agent passes the guide.

## Watch loop: Big Dad improves itself (with a yes)

1. At the end of an audit run `bigdad.py watch`: the hook logs every tool output over ~5k tokens (tool and size,
   never content) for the rest of the session, its subagents' too (tagged by agent type; `notes` shows their share).
2. When you see waste or a way Big Dad could do better, run `bigdad.py note "what happened, what to change"` right
   then. No paths or secrets.
3. Next audit: start with `fresh`, `notes` and `self`. Each note becomes a change only with the user's yes, then a
   test, a live proof in the mode it affects, and a measurement.
4. From a local directory marketplace the plugin is read in place (hook edits are live in every session; new hook
   registrations need a new session or `/reload-plugins`); from GitHub it is a cache copy (bump the version,
   `claude plugin update bigdad@<marketplace>` (`claude plugin list` shows the name; `bigdad@bigdad` from the bigdad
   repo), then a new session or `/reload-plugins`). `health` says which applies.
