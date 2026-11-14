<p align="center">
  <img src="assets/hero.jpg" width="100%" alt="Big Dad, the token watchdog for Claude Code">
</p>

<h1 align="center">🥛 Big Dad</h1>

<p align="center">
  The token watchdog for Claude Code<br>
  Plugin version 2.0.8 · MIT · Windows, macOS, Linux
</p>

## Watch the ad

<!-- VIDEO: paste the user-attachments URL on the next line -->


<!-- POSTER START: stand-in until the video is uploaded. Delete from here to POSTER END once the URL line above is filled in. -->
<p align="center">
  <a href="#watch-the-ad"><img src="assets/poster.jpg" width="100%" alt="Watch the ad. Every call re-sends everything."></a>
</p>
<!-- POSTER END -->

<p align="center">
  <img src="assets/preview-loop.webp" width="80%" alt="Silent loop from the ad. Same file read 3 times: on Haiku $0.0095 without Big Dad, $0.0048 with, -49.4%. On Sonnet $0.1745 to $0.0819, -53.1%.">
</p>

## What it is

> Dad went out for milk. He never came home.<br>
> **Big Dad's home.** I'm your token watchdog. I brought the milk.

Every time you hit enter, Claude Code sends the whole conversation again. Big Dad is a Claude Code plugin that watches what each call re-sends, stops a second read of a file that is already in context, trims noisy output and slims the compact. It changes no setting without your yes. It runs on your machine, uses the Python standard library only and has no telemetry.

<p align="center">
  <img src="assets/01-the-leak.jpg" width="80%" alt="The leak: the same file read, read again and read again. $0.0095 per run without Big Dad, Haiku, 10 runs a side, 12 Nov 2026, measured on v2.0.7.">
</p>

## Install

```
claude plugin marketplace add louievika/lab
claude plugin install bigdad@lab
```

Then start a new Claude Code session and say `big dad`.

Needs Python 3 (3.9 or newer). Update: `claude plugin update bigdad@lab`. Remove: `claude plugin uninstall bigdad@lab`.

## What it does

<p align="center">
  <img src="assets/02-watch-block-yes.jpg" width="80%" alt="01 Watch: every call, every helper agent. 02 Block: repeat reads, noisy output, slim compact. 03 Your yes: no setting changes without it.">
</p>

| | |
|---|---|
| **Watch** | Every call and every helper agent: what is re-sent, what loads at startup, which agents grow. |
| **Block** | A second read of an unchanged file that is still in context is stopped. Big, noisy command output is cut before it enters the chat; the full output is kept in a file. Before a compact, the summary is told what is dead weight. |
| **Your yes** | No setting changes without it. Every cut is backed up first and can be undone. Big Dad never deletes a skill, plugin or file of yours. |

Type `receipt` in any chat for that chat's receipt, read from its own log. It costs 0 tokens: the prompt never reaches the model.

## Measured results

<p align="center">
  <img src="assets/03-haiku-49-4.jpg" width="80%" alt="Same file read 3 times on Haiku: $0.0095 without Big Dad, $0.0048 with, -49.4%. 10 runs a side, median cost per run, 12 Nov 2026, measured on v2.0.7.">
</p>

| Case | Model | Runs | Without Big Dad | With Big Dad | Change |
|---|---|---|---|---|---|
| Same file read 3 times | Haiku | 10 a side | $0.0095 | $0.0048 | -49.4% |
| Same file read 3 times | Sonnet | 3 a side | $0.1745 | $0.0819 | -53.1% |
| Four-task set | Haiku | 5 a side | $0.0194 | $0.0160 | -17.5% |

Dollars per run, median, by Claude Code's own cost figure (not a bill). Measured on v2.0.7 on 12 Nov 2026. The first two rows come from a 7-case bench of 182 headless runs (Haiku 70 + 70, Sonnet 21 + 21), a fresh profile for every run. The four-task set was a separate run on the same day; its two run ranges do not touch.

<p align="center">
  <img src="assets/04-four-tasks-17-5.jpg" width="80%" alt="Four-task set on Haiku: $0.0194 with nothing installed, $0.0160 with Big Dad, -17.5%. 5 runs a side, each dot one run, run ranges do not touch.">
</p>

Everyday use, 6-10 Nov 2026 (90 chats, Big Dad's own count, versions 1.0 to 2.0.3): Big Dad cost 84k tokens and saved 577k.

<p align="center">
  <img src="assets/05-receipt.jpg" width="80%" alt="Big Dad receipt. Everyday use 6-10 Nov 2026: Big Dad cost 84k tokens, saved 577k tokens. Light tasks: no measurable change, measured on v2.0.7.">
</p>

### What it does not do

- **Light tasks:** no measurable change. In the light-chat case Big Dad stayed silent.
- **Helper agents:** the helper-agent case cost about 7% more dollars with Big Dad (Haiku +7.0%, Sonnet +7.2%, median; the run ranges overlap).
- **Whole suite on Haiku:** all 7 cases added up came out -9.0% in dollars, but the run ranges overlap, so the difference is inside the noise.
- **Noisy output:** that case came out even; the output shrink did not fire in that test.
- No saving is claimed for helper agents or compacts.
- Limits of the bench: scripted cases, not real work; one Windows PC; Haiku and Sonnet only, no Opus.

## Check the numbers yourself

- Type `receipt` in a chat. It reads that chat's own log: input split into cache reads, cache writes and new tokens, plus output.
- Say `big dad` for the audit. Every number it shows is labelled verified, estimated or not measured.
- Run the read-guard test on your own account. It runs short headless Claude Code sessions, the same file read twice, guard off and on:

  ```
  sh bigdad/skills/bigdad/scripts/bigdad.sh proof read --model haiku
  ```

  (from a clone of this repo; it spends a little of your own usage).

## Version and licence

Big Dad 2.0.8 · MIT · by LouieVika

<p align="center">
  <img src="assets/06-end-card.jpg" width="80%" alt="End card: Big Dad's home. Big Dad for Claude Code. github.com/louievika/lab">
</p>

<p align="center"><b>Built in silence, worn in gold. One of one. One and done.</b></p>
