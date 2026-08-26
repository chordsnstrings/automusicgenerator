"""The studio console — back of house.

The day page shows you five songs. This shows you the machine that made them:
which themes the Scout actually found and from which feed, what the Director
specified, what each brain was asked and how long it took, every clip including
the nine that did not ship and exactly why, and what the codex has learned.

The reason this exists as pages rather than log files: an unattended system you
cannot see is one you have to trust. Being able to open a run from three weeks
ago and read the rejection reasons is what turns "it made five songs" into
"I know why it made these five".

Rendered server-side as plain HTML. No build step, no framework, no client
state — a console that breaks when a bundle fails to load is worse than no
console.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime

CSS = """
:root{--bg:#f2f2ef;--card:#fff;--well:#f7f7f4;--ink:#16181b;--dim:#666c74;
      --faint:#8b9199;--rule:#dcdcd6;--hot:#b4560b;--ok:#2c6b33;--bad:#a3301d;
      --cool:#0a666b;color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--bg:#101215;--card:#171a1e;--well:#1b1f23;
      --ink:#e8eaed;--dim:#8b929b;--faint:#6b727b;--rule:#2b3037;--hot:#e9a13b;
      --ok:#68b36e;--bad:#e8705a;--cool:#43bab4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:14px/1.6 ui-monospace,"SF Mono",Menlo,monospace}
.wrap{max-width:78rem;margin:0 auto;padding:1.5rem 1.25rem 5rem}
nav{display:flex;gap:.15rem;flex-wrap:wrap;border-bottom:1px solid var(--rule);
    margin-bottom:1.75rem;padding-bottom:.6rem;align-items:baseline}
nav .brand{font-weight:700;font-size:1rem;letter-spacing:-.02em;margin-right:1.25rem}
nav a{color:var(--dim);text-decoration:none;padding:.3rem .6rem;border-radius:3px;
      font-size:.82rem}
nav a:hover{color:var(--ink);background:var(--well)}
nav a[aria-current=page]{color:var(--hot);background:var(--well)}
h1{font-size:1.35rem;letter-spacing:-.02em;margin:0 0 .3rem}
h2{font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);
   margin:2.25rem 0 .7rem;font-weight:500}
h2:first-of-type{margin-top:1.25rem}
.sub{color:var(--dim);font-size:.82rem;margin:0 0 1.5rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(7rem,1fr));
       border:1px solid var(--rule);border-radius:4px;background:var(--card);
       overflow:hidden;margin-bottom:1.25rem}
.stats div{padding:.8rem 1rem;border-right:1px solid var(--rule)}
.stats div:last-child{border-right:0}
.stats b{display:block;font-size:1.3rem;letter-spacing:-.02em;line-height:1.2}
.stats span{color:var(--faint);font-size:.65rem;letter-spacing:.12em;
            text-transform:uppercase}
.note{border-left:2px solid var(--hot);padding:.2rem 0 .2rem 1rem;margin:0 0 1.5rem;
      color:var(--dim)}
.note span{display:block;color:var(--faint);font-size:.65rem;letter-spacing:.12em;
           text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:.8rem;
      background:var(--card);border:1px solid var(--rule);border-radius:4px}
th{text-align:left;font-weight:500;color:var(--faint);font-size:.62rem;
   letter-spacing:.13em;text-transform:uppercase;padding:.6rem .75rem;
   border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:.55rem .75rem;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:0}
tr:hover td{background:var(--well)}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th.num{text-align:right}
td.w{max-width:26rem;white-space:normal;color:var(--dim);line-height:1.45}
td.tight{white-space:nowrap}
/* Browser-drawn <audio> controls take their palette from color-scheme above and
   nothing else. Chrome sheds the timer, then the scrub bar, then the play
   button itself as the element narrows — at 44px, which is what a phone-width
   table gives it, nothing is drawn at all. So the width is a floor rather than
   a preference, and the table is allowed to scroll instead. */
audio{max-width:100%;vertical-align:middle}
td audio{width:15rem;min-width:15rem;max-width:none;height:2rem}
.tw{overflow-x:auto}
.tw table{width:auto;min-width:100%}
.song{background:var(--card);border:1px solid var(--rule);border-radius:4px;
      padding:.9rem 1rem}
.song .ttl{font-weight:700;letter-spacing:-.01em}
.rate{display:flex;gap:.2rem;flex-wrap:wrap;align-items:center;margin:.5rem 0 0}
.rate button{font:inherit;font-size:.7rem;padding:.15rem .4rem;background:var(--card);
      color:var(--dim);border:1px solid var(--rule);border-radius:2px;cursor:pointer}
.rate button:hover{color:var(--ink);border-color:var(--hot)}
.rate button[aria-pressed=true]{color:var(--hot);border-color:var(--hot)}
.rate button.clear{margin-left:.4rem;border-color:transparent;text-decoration:underline;color:var(--faint)}
.rate .done{color:var(--faint);font-size:.7rem;margin-left:.35rem}
a{color:var(--hot)}
a.q{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--rule)}
a.q:hover{border-color:var(--hot)}
.pill{display:inline-block;padding:.08rem .45rem;border-radius:2px;font-size:.66rem;
      letter-spacing:.06em;text-transform:uppercase;border:1px solid currentColor}
.p-ok{color:var(--ok)} .p-bad{color:var(--bad)} .p-hot{color:var(--hot)}
.p-dim{color:var(--faint)} .p-cool{color:var(--cool)}
.mini{color:var(--faint);font-size:.72rem}
.empty{color:var(--faint);padding:1.5rem;text-align:center;background:var(--card);
       border:1px dashed var(--rule);border-radius:4px;font-size:.82rem}
pre{background:var(--well);border:1px solid var(--rule);border-radius:4px;
    padding:.85rem 1rem;overflow-x:auto;font-size:.75rem;line-height:1.65;
    color:var(--dim);margin:0 0 1rem}
.bar{height:3px;background:var(--rule);border-radius:2px;overflow:hidden;
     min-width:3rem;margin-top:.3rem}
.bar i{display:block;height:100%;background:var(--hot)}
.flow{display:flex;gap:.3rem;flex-wrap:wrap;margin-bottom:1.25rem}
.flow div{flex:1 1 6rem;padding:.5rem .7rem;background:var(--card);
          border:1px solid var(--rule);border-radius:3px;font-size:.7rem}
.flow div.done{border-color:var(--ok);color:var(--ok)}
.flow div.now{border-color:var(--hot);color:var(--hot)}
.flow div b{display:block;font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;
            color:var(--faint);margin-bottom:.15rem}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(21rem,1fr));gap:1rem}
@media(max-width:720px){.wrap{padding:1rem .75rem 3rem} td.w{max-width:14rem}}
"""

NAV = [("/", "Overview"), ("/runs", "Runs"), ("/agents", "Agents"),
       ("/genres", "Genres"), ("/codex", "Codex"), ("/files", "Files")]

# Progressive enhancement, and it must stay that way: every rating form posts and
# redirects on its own. All this buys is not reloading the page — which matters
# only because a reload stops whatever is playing mid-song. Do not make it
# required, and do not port the day page's script: that one seeds from
# localStorage and hydrates over the network because it has no database behind
# it. This page renders the recorded rating server-side, so it needs neither.
RATE_JS = """
document.addEventListener('submit', async ev => {
  const form = ev.target.closest('form[data-rate]');
  if (!form || !ev.submitter) return;          // no submitter: let the browser post
  ev.preventDefault();
  const id = Number(form.dataset.rate);
  const out = form.querySelector('.done');
  const buttons = form.querySelectorAll('button[name=rating]');

  if (ev.submitter.name === 'clear') {
    out.textContent = 'clearing…';
    try {
      const r = await fetch('/ratings/' + id, {method: 'DELETE'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      // Only once the server has agreed. A clear that failed and looks like it
      // worked hides a value that is still steering the codex, and the control
      // that would fix it is the one thing this branch removes.
      buttons.forEach(b => b.setAttribute('aria-pressed', 'false'));
      out.textContent = '';
      ev.submitter.remove();
    } catch (e) {
      out.textContent = 'could not clear (' + e.message + ') — try again';
    }
    return;
  }

  const score = Number(ev.submitter.value);
  buttons.forEach(b =>
    b.setAttribute('aria-pressed', Number(b.value) === score ? 'true' : 'false'));
  out.textContent = 'saving…';
  try {
    const r = await fetch('/ratings', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({clip_id: id, rating: score})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    out.textContent = 'rated ' + score + '/10';
    // The server renders the clear control only for a song that already has a
    // rating, which is right for a page load and wrong for the second after a
    // mis-tap — the one second anybody wants an undo. So the branch that
    // creates a rating creates the control, mirroring the branch above that
    // removes it. Without this the only way to reach an undo is a reload, and
    // avoiding the reload is the entire reason this script exists.
    if (!form.querySelector('button[name=clear]')) {
      const undo = document.createElement('button');
      undo.type = 'submit';
      undo.name = 'clear';
      undo.value = '1';
      undo.className = 'clear';
      undo.textContent = 'clear';
      out.before(undo);
    }
  } catch (e) {
    out.textContent = 'could not save (' + e.message + ') — reload and try again';
  }
});
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def page(title: str, body: str, current: str = "/", *, script: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if href == current else ""}>{esc(label)}</a>'
        for href, label in NAV)
    tail = f"<script>{script}</script>" if script else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — The Daily Five</title><style>{CSS}</style></head><body>
<div class="wrap"><nav><span class="brand">The Daily Five</span>{links}</nav>
{body}</div>{tail}</body></html>"""


def stats(*pairs) -> str:
    cells = "".join(f"<div><b>{esc(v)}</b><span>{esc(k)}</span></div>" for k, v in pairs)
    return f'<div class="stats">{cells}</div>'


def pill(text: str, kind: str = "dim") -> str:
    return f'<span class="pill p-{kind}">{esc(text)}</span>'


def table(headers: list[str], rows: list[list[str]], *, empty: str = "nothing here yet",
          num_cols: set[int] | None = None) -> str:
    if not rows:
        return f'<div class="empty">{esc(empty)}</div>'
    num_cols = num_cols or set()
    head = "".join(f'<th class="{"num" if i in num_cols else ""}">{esc(h)}</th>'
                   for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    # Wrapped so a table that cannot fit scrolls inside its own box. Without it
    # a single wide cell — a player has a floor it will not go below — pushes
    # the whole page sideways, and the nav goes with it.
    return (f'<div class="tw"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def ago(when: datetime | None) -> str:
    if when is None:
        return "—"
    from .._compat import now_utc
    delta = (now_utc() - _aware(when)).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= n:
            return f"{int(delta // n)}{unit} ago"
    return "just now"


def _aware(dt: datetime) -> datetime:
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ms(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "—"
    return f"{v / 1000:.1f}s" if v >= 1000 else f"{v}ms"


def dur(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    return f"{s // 60}:{s % 60:02d}"


def jsonblock(obj, limit: int = 4000) -> str:
    text = json.dumps(obj, indent=2, default=str, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + f"\n… {len(text) - limit} more characters"
    return f"<pre>{esc(text)}</pre>"


def bar(value: float, of: float) -> str:
    pct = 0 if not of else max(0, min(100, value / of * 100))
    return f'<div class="bar"><i style="width:{pct:.0f}%"></i></div>'
