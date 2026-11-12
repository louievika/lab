"""Big Dad's one native-lever table: each Big Dad part, the Claude Code lever that does (or could) do its job, where
that lever exists, the changelog / release-note words that mean "re-check this part", and what native lacks.

`fresh` (bd_fresh.RETIRE) and `versus` (bd_versus.NATIVE / NATIVE_LACKS) both read this table, so a retire note can't
update one and not the other. A RE-CHECK hit means look again with a live run, never delete on a changelog line.
Surfaces: CLI = terminal; Desktop = Claude desktop app, Code tab; IDE = VS Code / JetBrains; Web = claude.ai/code
cloud sessions; Chat = claude.ai / phone chat (no hooks, no local logs). From the docs; `fresh` re-checks.
"""
import re

# The re-check words include the log and hook field names Big Dad parses (GROK-ALIGN §4: permissionDecision, continue,
# decision, custom_instructions, compact_boundary, local-command-stdout, truncatedByTokenCap, rate_limits,
# agent_transcript_path, ...). A line naming one means a parser may go quiet: re-check, don't delete.
TABLE = [
    # (Big Dad part, native lever today, surfaces with the lever, re-check words, what native lacks or "")
    ("read guard (no second copy of an unchanged file)",
     "none since 2.1.317 removed the Read 'file unchanged' stub", "CLI, Desktop, IDE",
     r"unchanged|re-?read|already in (your )?context|duplicate read|permissionDecision|truncatedByTokenCap|numLines|totalLines",
     "a block on re-reading an unchanged file already in context"),
    ("context guard (warn past the per-call guide)",
     "autoCompactWindow / /autocompact (compacts; doesn't warn or coach)", "CLI, Desktop, IDE",
     r"autoCompactWindow|autocompact|context warning|context (limit|window) (warn|notice)|systemMessage|additionalContext",
     "a warning when one call re-sends more than a guide"),
    ("main-chat pause (ends a turn far past the guide)",
     "none (auto-compact never stops a turn)", "CLI, Desktop, IDE",
     r"stopReason|\"continue\"|continue: ?false|hook_stopped_continuation", ""),
    ("slim compact (PreCompact instructions)",
     "/compact <text> (manual only; auto-compact takes no user text)", "CLI, Desktop, IDE",
     r"PreCompact|compact(ion)? (instruction|prompt|summary)|custom_instructions|compact_boundary|local-command-stdout", ""),
    ("redirect detector ('scrap this, start over')",
     "none", "CLI, Desktop, IDE",
     r"UserPromptSubmit|hook_system_message|(start over|scrap|abandon|change of plan).{0,40}(detect|notice|prompt)|plan (redirect|reset)", ""),
    ("agent monitor (agents.jsonl, size per agent)",
     "/usage per-subagent attribution (this session, no history, no flags)", "CLI, Desktop, IDE",
     r"SubagentStart|SubagentStop|agent (usage|cost|attribution)|per-agent|agent_transcript_path",
     "a watch on a subagent's own context size"),
    ("agent hard stop (agent-stop: deny past the cap, handoff)",
     "autoCompactWindow in the subagent's frontmatter (compacts the agent; new sessions only)", "CLI, Desktop, IDE",
     r"autoCompactWindow|subagent (frontmatter|compact|context)|permissionDecision", ""),
    ("loop note (same call 4+ times, watched sessions)",
     "none", "CLI, Desktop, IDE", r"loop detect|tool[- ]call loop|stuck (in )?a loop|repeated (identical )?tool calls", ""),
    ("watch: big tool outputs",
     "bashOutputMaxChars, MAX_MCP_OUTPUT_TOKENS (cap; don't report)", "CLI, Desktop, IDE, Web",
     r"bashOutputMaxChars|MAX_MCP_OUTPUT_TOKENS|tool (output|result) (limit|truncat)", ""),
    ("status line (size, tokens, plan %)",
     "statusLine command (native hook point; Big Dad supplies the script)", "CLI",
     r"statusLine|status line|rate_limits|context_window", ""),
    ("receipt (each compact's call before -> after, from the log)",
     "/cost (session totals), /usage (plan %): no per-compact drop", "CLI, Desktop, IDE",
     r"/cost|rate_limits|cache_(creation|read)_input_tokens|usage (row|field)|\bdecision\b",
     "a per-chat receipt of each compact's measured drop"),
    ("models / modes / report (tokens by model x mode over time)",
     "/usage (per skill/subagent/plugin/MCP), /context (this session only)", "CLI, Desktop, IDE",
     r"/usage|/context|usage report|attribution|entrypoint", ""),
    ("skills cut advice",
     "skillOverrides, disable-model-invocation (name-only or off in the list)", "CLI, Desktop, IDE, Web",
     r"skillOverrides|disable-model-invocation|skill (list|listing)", ""),
    ("MCP tool-name cost",
     "ENABLE_TOOL_SEARCH (deferred names), connector toggles at claude.ai", "CLI, Desktop, IDE, Web, Chat",
     r"tool search|ENABLE_TOOL_SEARCH|deferred tool names", ""),
]

RETIRE = [(part, lever, surfaces, words) for part, lever, surfaces, words, _ in TABLE]
NATIVE = [(lever, part) for part, lever, _, _, _ in TABLE]
NATIVE_LACKS = tuple(lacks for *_, lacks in TABLE if lacks)

# Hook and log field names Big Dad parses. A changelog line that hits a part only through one of these is a parser
# re-check (run the tests), not a sign that Claude Code now does the part's job. Before 2.0.2 every hit said "may
# retire": "Fixed ... UserPromptSubmit hook ..." flagged the redirect detector for retirement.
PARSER = re.compile(r"UserPromptSubmit|hook_system_message|permissionDecision|truncatedByTokenCap|numLines|totalLines|"
                    r"systemMessage|additionalContext|stopReason|\"continue\"|continue: ?false|hook_stopped_continuation|"
                    r"PreCompact|custom_instructions|compact_boundary|local-command-stdout|SubagentStart|SubagentStop|"
                    r"agent_transcript_path|rate_limits|context_window|cache_(creation|read)_input_tokens|\bdecision\b|"
                    r"statusLine|entrypoint", re.I)


def verdict(words, lines):
    """'keep', or the re-check note for the first line that hits `words`: 'may retire' only when a hit is more than a
    field name Big Dad reads."""
    hook = None
    for line in lines:
        hits = [m.group(0) for m in re.finditer(words, line, re.I)]
        if not hits:
            continue
        if any(not PARSER.fullmatch(h) for h in hits):
            return f"RE-CHECK: a new line mentions it ({line[:70]}...) -> Big Dad may retire this part"
        hook = hook or line
    if hook:
        return f"hook check: a field it reads changed ({hook[:60]}...) -> run the tests; not a retire"
    return "keep"
