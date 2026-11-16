# 🥛 Big Dad

The token watchdog for Claude Code. Plugin version 3.0.1.

Every time you hit enter, Claude Code sends the whole conversation again. Big Dad watches every call and every helper agent, blocks the repeat read, trims noisy output and slims the compact. It changes no setting without your yes.

<p align="center">
  <img src="../assets/02-watch-block-yes.jpg" width="80%" alt="01 Watch: every call, every helper agent. 02 Block: repeat reads, noisy output, slim compact. 03 Your yes: no setting changes without it.">
</p>

## Install

```
claude plugin marketplace add louievika/lab
claude plugin install bigdad@lab
```

Start a new Claude Code session so the hooks load, then say `big dad`.

- Update: `claude plugin update bigdad@lab`
- Remove: `claude plugin uninstall bigdad@lab`
- Needs Python 3 (3.9 or newer). Windows, macOS, Linux.

## What you can say

| Say or type | What happens |
|---|---|
| `big dad` | The audit: what each call re-sends, what loads at startup, which skills and tools never get used, and the cuts it proposes. Each cut waits for your yes. |
| `receipt` | This chat's short receipt, read from its own log. 0 tokens: the prompt never reaches the model. `receipt full` prints every line. |

The same tools run from the command line, from this folder:

```
sh skills/bigdad/scripts/bigdad.sh all 10       # full audit, last 10 sessions
sh skills/bigdad/scripts/bigdad.sh modes        # overhead by execution mode
sh skills/bigdad/scripts/bigdad.sh agents       # helper agents: running, size now, flags
sh skills/bigdad/scripts/bigdad.sh health       # security and upkeep proposals, read only
sh skills/bigdad/scripts/bigdad.sh guide 300k   # a cost guide for this project; guide auto drops it
sh skills/bigdad/scripts/bigdad.sh fresh        # what changed since the last check
sh skills/bigdad/scripts/bigdad.sh setup        # tunes Big Dad to this machine
sh skills/bigdad/scripts/bigdad.sh report       # local HTML page, prints its path
```

Each hook can be switched off with an environment variable, for example `BIGDAD_READ_GUARD=off`. `BIGDAD_AGENT_GUARD=off` removes the helper-agent notes. `BIGDAD_LEARN=off` skips the learning period on a new install.

Measured results and their limits are on the [main page](../README.md#measured-results).

## Licence

MIT. See [LICENSE](LICENSE).
