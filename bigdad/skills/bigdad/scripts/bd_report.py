"""Big Dad `report`: one self-contained local HTML page for people who use Claude visually.

Charts (inline SVG, no scripts, no network, no server): tokens per call over time, startup load
by source, models x modes, agents, health flags, the profile. No file paths, session ids or user
names on the page; counts, sizes and short labels only. Prints the page's path.

  python bigdad.py report [HOURS] [--out FILE]
"""
import html, io, json, os, re, time
from collections import defaultdict
from contextlib import redirect_stdout
import bd_health as H
import bd_models as M
import bd_setup as S

OUT = os.path.join(H.CLAUDE_DIR, "bigdad", "report.html")
PALETTE = ["#4F6BED", "#E08A2E", "#2BA37A", "#C8477D", "#7A5AC8", "#8A8F98"]


def esc(s):
    return html.escape(str(s))


def calls_over_time(proj, since):
    """(ts, input tokens, model family, mode) per distinct API call in logs touched since `since`."""
    pts = []
    import glob
    for p in glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(p) < since:
                continue
        except OSError:
            continue
        entry, seen = None, set()
        meta = M.meta_of(p) if "subagents" in p.replace("\\", "/") else {}
        rows = []
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if entry is None and '"entrypoint"' in line:
                    m = re.search(r'"entrypoint":\s*"([^"]+)"', line)
                    entry = m.group(1) if m else None
                if '"usage"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("type") != "assistant":
                    continue
                msg = r.get("message") or {}
                u, mid = msg.get("usage") or {}, msg.get("id")
                if not u or mid in seen:
                    continue
                seen.add(mid)
                t = M.ts(r.get("timestamp") or "")
                n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                if t >= since and n:
                    rows.append((t, n, M.short_model(msg.get("model"))))
        mode = M.mode_of(p, entry, meta)
        pts += [(t, n, fam, mode) for t, n, fam in rows]
    pts.sort()
    if len(pts) > 1500:                       # keep the page light: even sampling
        step = len(pts) / 1500
        pts = [pts[int(i * step)] for i in range(1500)]
    return pts


def svg_scatter(pts, w=880, h=260):
    if not pts:
        return "<p class='muted'>no calls in this window</p>"
    t0, t1 = pts[0][0], max(pts[-1][0], pts[0][0] + 60)
    ymax = max(n for _, n, _, _ in pts) * 1.08
    fams = sorted({f for _, _, f, _ in pts})
    color = {f: PALETTE[i % len(PALETTE)] for i, f in enumerate(fams)}
    L, B, T, R = 64, 36, 12, 12
    out = [f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='input tokens per API call over time'>"]
    for i in range(5):
        y = T + (h - T - B) * i / 4
        v = ymax * (1 - i / 4)
        out.append(f"<line x1='{L}' y1='{y:.0f}' x2='{w - R}' y2='{y:.0f}' class='grid'/>"
                   f"<text x='{L - 6}' y='{y + 4:.0f}' text-anchor='end' class='tick'>{v / 1000:,.0f}k</text>")
    for i in range(5):
        x = L + (w - L - R) * i / 4
        tt = time.strftime("%H:%M", time.localtime(t0 + (t1 - t0) * i / 4))
        out.append(f"<text x='{x:.0f}' y='{h - 10}' text-anchor='middle' class='tick'>{tt}</text>")
    for t, n, f, mode in pts:
        x = L + (w - L - R) * (t - t0) / (t1 - t0)
        y = T + (h - T - B) * (1 - n / ymax)
        shape = "circle" if mode in ("interactive", "headless") else "rect"
        if shape == "circle":
            out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.6' fill='{color[f]}' opacity='.75'><title>{mode}, {f}: {n:,} tokens</title></circle>")
        else:
            out.append(f"<rect x='{x - 2.4:.1f}' y='{y - 2.4:.1f}' width='4.8' height='4.8' fill='{color[f]}' opacity='.75'><title>{mode}, {f}: {n:,} tokens</title></rect>")
    out.append("</svg>")
    legend = " ".join(f"<span class='key'><i style='background:{color[f]}'></i>{esc(f)}</span>" for f in fams)
    legend += " <span class='key'><i class='dot'></i>interactive / headless</span> <span class='key'><i class='sq'></i>agents, forks, workflows</span>"
    return "".join(out) + f"<div class='legend'>{legend}</div>"


def svg_bars(items, w=880, unit="tokens", fmt=lambda v: f"{v:,.0f}"):
    """Horizontal bars for [(label, value)], biggest first."""
    items = [(l, v) for l, v in items if v]
    if not items:
        return "<p class='muted'>nothing measured</p>"
    items.sort(key=lambda kv: -kv[1])
    vmax = max(v for _, v in items)
    rh, L = 22, 230
    h = rh * len(items) + 8
    out = [f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='bar chart'>"]
    for i, (label, v) in enumerate(items):
        y = 4 + i * rh
        bw = (w - L - 90) * v / vmax
        out.append(f"<text x='{L - 8}' y='{y + 15}' text-anchor='end' class='lab'>{esc(label[:34])}</text>"
                   f"<rect x='{L}' y='{y + 3}' width='{bw:.1f}' height='{rh - 7}' rx='3' fill='{PALETTE[0]}'/>"
                   f"<text x='{L + bw + 6:.1f}' y='{y + 15}' class='val'>{esc(fmt(v))} {esc(unit)}</text>")
    out.append("</svg>")
    return "".join(out)


def table(headers, rows):
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def models_by_mode(logs):
    cell = defaultdict(lambda: [0, 0, 0])
    for L in logs:
        for model, (c, i, o) in L["per"].items():
            x = cell[(model, L["mode"])]
            x[0] += c; x[1] += i; x[2] += o
    return cell


def startup_parts(agent):
    try:
        sess = agent.sessions()
        for p in sess[:5]:
            r = agent.load_parts(p)
            if r:
                return r
    except Exception:
        pass
    return None


def health_lines(cwd):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            H.FINDINGS.clear()
            H.cmd_health(cwd)
    except Exception as e:
        return [f"health check failed to run: {type(e).__name__}"]
    lines = []
    for l in buf.getvalue().splitlines():
        l = l.strip()
        if l.startswith(("[sec]", "[fix]", "[ok]", "[info]")):
            lines.append(scrub(l))
    return lines


def scrub(s):
    """No home path, user name, e-mail or session id on the page."""
    home = os.path.expanduser("~")
    user = os.path.basename(home)
    for mark in (home.replace("\\", "/"), home, home.replace("/", "\\")):
        s = s.replace(mark, "~")
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", s, flags=re.I)
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", s)
    if user and len(user) > 2:
        s = re.sub(re.escape(user), "~", s, flags=re.I)
    return s


def agent_rows(hours):
    import bd_agents as A
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            A.cmd_agents(hours)
    except Exception:
        return []
    rows = []
    for l in buf.getvalue().splitlines():
        parts = l.split()
        if parts and parts[0] in ("running", "done") and len(parts) >= 6:
            rows.append([parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], " ".join(parts[6:])])
    flags = [scrub(l.strip()) for l in buf.getvalue().splitlines() if l.strip().startswith(("flag:", "ok:"))]
    return rows, flags


CSS = """
:root{--bg:#fff;--fg:#1b1f27;--mut:#6b7280;--line:#e5e7eb;--card:#f8fafc}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--fg:#e6e8ee;--mut:#9aa1ad;--line:#2a2f3a;--card:#161b24}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px}
.sub,.muted{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--line);stroke-width:1}.tick,.lab,.val{fill:var(--mut);font-size:11px}
.lab{fill:var(--fg)}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600}td:nth-child(n+4),th:nth-child(n+4){text-align:right}
.legend{margin-top:6px;font-size:12px;color:var(--mut)}.key{margin-right:12px}.key i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:-1px}
.key i.dot{background:var(--mut)}.key i.sq{background:var(--mut);border-radius:2px}
ul.flags{padding-left:18px;margin:6px 0}ul.flags li{margin:3px 0}.sec{color:#C8477D}.fix{color:#E08A2E}.ok{color:#2BA37A}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px 18px}.kv b{display:block;font-size:12px;color:var(--mut);font-weight:600}
"""


def cmd_report(args):
    hours = float(next((a for a in args if re.fullmatch(r"\d+(\.\d+)?", a)), 24))
    out = OUT
    if "--out" in args:
        out = args[args.index("--out") + 1]
    cwd = os.getcwd()
    prof = S.load() or {}
    proj = os.path.join(H.CLAUDE_DIR, "projects")
    since = time.time() - hours * 3600
    pts = calls_over_time(proj, since)
    logs = M.scan(proj, since)
    cell = models_by_mode(logs)
    total_in = sum(v[1] for v in cell.values())
    # startup load
    import bigdad
    bigdad.REDACT = True
    parts_html = "<p class='muted'>no session with a measured first call</p>"
    agent = bigdad.Claude()
    sp = startup_parts(agent) if agent.present() else None
    if sp:
        total, parts, _ = sp
        parts_html = (f"<p class='sub'>newest measured first call: {total:,} tokens. Shares below are by size (≈ 4 chars per token).</p>"
                      + svg_bars([(k, v // 4) for k, v in parts.items()], unit="≈tokens"))
    # agents
    ar = agent_rows(hours)
    arows, aflags = (ar if ar else ([], []))
    surfaces = ", ".join(f"{k} ×{v}" for k, v in (prof.get("surfaces") or {}).items()) or "not profiled yet"
    hl = health_lines(cwd)
    def cls(l):
        return "sec" if l.startswith("[sec]") else "fix" if l.startswith("[fix]") else "ok"
    page = f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Big Dad report</title><style>{CSS}</style></head><body><main>
<h1>Big Dad report</h1>
<p class='sub'>{esc(time.strftime('%Y-%m-%d %H:%M %Z'))} · last {hours:g} h · surfaces seen (7 days): {esc(surfaces)} · all numbers from Claude Code's own logs (verified: API usage) unless marked ≈</p>
<h2>Tokens per call over time</h2><div class='card'>{svg_scatter(pts)}<p class='muted'>Every call re-sends the whole context. A staircase means the session kept growing; a drop is a /compact or a fresh session. Guard at {prof.get('guard_at', 130000) // 1000}k.</p></div>
<h2>Startup load by source</h2><div class='card'>{parts_html}</div>
<h2>Models × modes</h2><div class='card'>{table(["model", "mode", "calls", "input tokens", "output", "input/call", "share"],
    [[m, mode, f"{c:,}", M.big(i), M.k(o), M.k(i // max(c, 1)), f"{i / total_in:.0%}" if total_in else "-"]
     for (m, mode), (c, i, o) in sorted(cell.items(), key=lambda kv: -kv[1][1])[:16]])}
<p class='muted'>Total input: {M.big(total_in)} in {len(logs)} logs. The mode with the most input tokens is where cuts pay most.</p></div>
<h2>Agents</h2><div class='card'>{table(["state", "type", "model", "calls", "last call", "total", "flags"], arows) if arows else "<p class='muted'>no agents logged in this window (SubagentStart/Stop hooks log them to agents.jsonl)</p>"}
{"<ul class='flags'>" + "".join(f"<li>{esc(f)}</li>" for f in aflags) + "</ul>" if aflags else ""}</div>
<h2>Health flags</h2><div class='card'><ul class='flags'>{"".join(f"<li class='{cls(l)}'>{esc(l)}</li>" for l in hl) or "<li class='muted'>health check produced no lines</li>"}</ul>
<p class='muted'>Proposals only. Nothing is changed without a yes.</p></div>
<h2>Profile (Big Dad's own knobs)</h2><div class='card kv'>
<div><b>real window</b>{prof.get('window', 0) // 1000:,}k ({esc(prof.get('window_source', '?'))})</div>
<div><b>guard per call</b>{prof.get('guard_at', 130000) // 1000:,}k ({esc(prof.get('guard_source', 'default'))})</div>
<div><b>big model</b>{esc(prof.get('big_model', '?'))} · max {esc(prof.get('max_big_agents', 1))} at a time</div>
<div><b>Claude Code</b>{esc(prof.get('claude') or '?')} · {esc(prof.get('os', '?'))}</div>
<div><b>setup</b>{esc(prof.get('skills', '?'))} skill folders · {len(prof.get('plugins') or {})} plugins · {esc(prof.get('mcp', '?'))} MCP servers</div>
<div><b>hooks per tool call</b>{esc((prof.get('hooks_per_tool_call') or {}).get('count', '?'))} commands · ≈{esc((prof.get('hooks_per_tool_call') or {}).get('ms', '?'))} ms</div>
<div><b>profiled</b>{esc((prof.get('at') or '?')[:16].replace('T', ' '))} {esc(prof.get('tz', ''))}</div>
</div>
<p class='muted' style='margin-top:30px'>Big Dad brings home the milk. Observe → note → propose → act only with a yes → prove → measure. No paths, ids or names on this page.</p>
</main></body></html>"""
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(scrub(page))
    print(f"🥛 == REPORT  written: {out}")
    print(f"  {len(pts)} calls plotted, {len(cell)} model×mode cells, {len(arows)} agents, {len(hl)} health lines. Open it in any browser; no server, no network.")
