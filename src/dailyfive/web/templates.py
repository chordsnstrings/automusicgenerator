"""The page delivered with each day's folder.

Five players and a rating control. The control posts to the same public
endpoint that receives Suno's callbacks, so there is exactly one public surface
to secure and nothing extra to run.

It has to work from a signed Spaces URL with no build step and no network
dependencies, so this is one self-contained string. It also has to be usable in
thirty seconds on a phone, because a rating control that is any slower than that
does not get used, and an unused rating control means the loop never closes.
"""

from __future__ import annotations

import html
import json
from datetime import date

CSS = """
:root{--bg:#f2f2ef;--card:#fff;--ink:#16181b;--dim:#666c74;--rule:#dcdcd6;
      --hot:#b4560b;--ok:#2c6b33;
      /* Native <audio> controls are drawn by the browser, not this stylesheet.
         Without color-scheme they stay light on a dark page — five white bars
         against dark cards. This is the only way to theme them. */
      color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--bg:#111316;--card:#181b1f;--ink:#e8eaed;
      --dim:#8b929b;--rule:#2b3037;--hot:#e9a13b;--ok:#68b36e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 ui-monospace,
     "SF Mono",Menlo,monospace;padding:2.5rem 1.25rem 5rem}
.wrap{max-width:46rem;margin:0 auto}
h1{font-size:1.5rem;letter-spacing:-.02em;margin:0 0 .25rem}
.sub{color:var(--dim);font-size:.8rem;margin:0 0 2rem}
.song{background:var(--card);border:1px solid var(--rule);border-radius:4px;
      padding:1.15rem 1.25rem;margin-bottom:1rem}
.hd{display:flex;justify-content:space-between;gap:1rem;align-items:baseline}
.ttl{font-weight:600;font-size:1.05rem}
.meta{color:var(--dim);font-size:.72rem;text-align:right;white-space:nowrap}
.tags{color:var(--dim);font-size:.75rem;margin:.4rem 0 .7rem}
audio{width:100%;margin:.5rem 0 .75rem}
.dl a{color:var(--hot);font-size:.75rem;margin-right:1rem;text-decoration:none;
      border-bottom:1px solid currentColor}
.rate{margin-top:.9rem;padding-top:.85rem;border-top:1px solid var(--rule)}
.rate span{color:var(--dim);font-size:.72rem;display:block;margin-bottom:.45rem}
/* A grid, not a wrapping flex row: flex-wrap leaves a ragged 8 + 2 on a phone
   with two stretched buttons, which is both ugly and harder to hit accurately.
   Ten equal columns, or a tidy 5 x 2 once they stop fitting. */
.btns{display:grid;grid-template-columns:repeat(10,1fr);gap:.3rem}
button{padding:.5rem 0;font:inherit;font-size:.85rem;
       background:transparent;color:var(--ink);border:1px solid var(--rule);
       border-radius:3px;cursor:pointer}
button:hover{border-color:var(--hot)}
button[aria-pressed=true]{background:var(--hot);border-color:var(--hot);color:#fff}
button:disabled{opacity:.5;cursor:default}
.done{color:var(--ok);font-size:.72rem;margin-top:.45rem;min-height:1.1em}
footer{color:var(--dim);font-size:.72rem;margin-top:2.5rem;border-top:1px solid var(--rule);
       padding-top:1rem}
@media(max-width:520px){
  body{padding:1.75rem 1rem 3rem}
  /* Stack the header: a title squeezed beside the meta column wraps to three
     words on three lines. */
  .hd{display:block}
  .meta{text-align:left;white-space:normal;margin-top:.3rem}
  .btns{grid-template-columns:repeat(5,1fr)}
  button{padding:.62rem 0}
}
"""

JS = """
const API=%(api)s;
const CARDS=[...document.querySelectorAll('[data-clip]')];

function mark(card,score,label){
  const out=card.querySelector('.done');
  card.querySelectorAll('button[data-score]').forEach(b=>{
    b.setAttribute('aria-pressed', Number(b.dataset.score)===Number(score)?'true':'false');});
  if(label!==null) out.textContent=label;
}

CARDS.forEach(card=>{
  const id=Number(card.dataset.clip);
  const out=card.querySelector('.done');
  // Show this browser's own last answer immediately, so the page is never blank
  // while the server round-trip is in flight. The server overrides it below.
  try{
    const prev=localStorage.getItem('rating:'+id);
    if(prev) mark(card,prev,'rated '+prev+'/10');
  }catch(e){}

  card.querySelectorAll('button[data-score]').forEach(btn=>{
    btn.addEventListener('click',async()=>{
      const score=Number(btn.dataset.score);
      mark(card,score,'saving…');
      try{
        const r=await fetch(API+'/ratings',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({clip_id:id,rating:score})});
        if(!r.ok) throw new Error('HTTP '+r.status);
        out.textContent='saved — '+score+'/10';
        try{localStorage.setItem('rating:'+id,String(score));}catch(e){}
      }catch(e){
        out.textContent='could not save ('+e.message+') — try again';
      }
    });
  });
});

// Hydrate from the server. localStorage only knows what THIS browser did, so a
// song rated on a phone would read as unrated on a laptop — and get rated twice.
(async()=>{
  if(!CARDS.length) return;
  const ids=CARDS.map(c=>c.dataset.clip).join(',');
  try{
    const r=await fetch(API+'/ratings?clip_ids='+encodeURIComponent(ids));
    if(!r.ok) return;
    const {ratings}=await r.json();
    CARDS.forEach(card=>{
      const v=ratings[card.dataset.clip];
      if(v==null) return;
      mark(card,v,'rated '+v+'/10');
      try{localStorage.setItem('rating:'+card.dataset.clip,String(v));}catch(e){}
    });
  }catch(e){/* offline is survivable — localStorage already filled in */}
})();
"""


def day_page(run_date: date, songs: list[dict], *, api_base: str,
             learning_note: str = "") -> str:
    """One self-contained page. ``songs`` carry signed URLs, not keys."""
    cards = "\n".join(_card(s) for s in songs)
    esc = html.escape
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Daily Five — {run_date.isoformat()}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>The Daily Five</h1>
<p class="sub">{run_date.strftime('%A %-d %B %Y')} · {len(songs)} songs ·
rate each one to close the loop{(' · ' + esc(learning_note)) if learning_note else ''}</p>
{cards}
<footer>
Ratings post to <code>{esc(api_base)}/ratings</code>. Nothing else on this page
talks to the network. WAV files are delivery masters — not loudness normalised.
MP3s are normalised to &minus;14&nbsp;LUFS.
</footer>
</div>
<script>{JS % {"api": json.dumps(api_base.rstrip('/'))}}</script>
</body></html>
"""


def _card(s: dict) -> str:
    esc = html.escape
    meta_bits = [b for b in (
        s.get("slot_type", "").upper() or None,
        f"{s['bpm']} BPM" if s.get("bpm") else None,
        s.get("key"),
        _dur(s.get("duration_s")),
    ) if b]
    links = []
    for label, key in (("WAV", "wav_url"), ("MP3", "mp3_url"), ("Lyrics", "lrc_url")):
        if s.get(key):
            links.append(f'<a href="{esc(s[key])}" download>{label}</a>')

    buttons = "".join(
        f'<button data-score="{n}" aria-pressed="false" '
        f'aria-label="Rate {n} out of 10">{n}</button>' for n in range(1, 11))

    # preload="metadata" costs a few KB per song and is what makes the player
    # show a real duration; with "none" every track reads 0:00 / 0:00 until
    # played, which looks broken on a page whose whole job is to be glanced at.
    audio = (f'<audio controls preload="metadata" src="{esc(s["mp3_url"])}"></audio>'
             if s.get("mp3_url") else
             '<p class="tags">audio unavailable — check the manifest</p>')

    return f"""<div class="song" data-clip="{int(s['clip_id'])}">
  <div class="hd">
    <div class="ttl">{esc(s.get('title') or 'Untitled')}</div>
    <div class="meta">{esc(' · '.join(meta_bits))}<br>{esc(s.get('persona') or '')}</div>
  </div>
  <div class="tags">{esc(s.get('theme') or '')}</div>
  {audio}
  <div class="dl">{' '.join(links)}</div>
  <div class="rate">
    <span>How good is this, 1&ndash;10?</span>
    <div class="btns">{buttons}</div>
    <div class="done" role="status"></div>
  </div>
</div>"""


def _dur(seconds) -> str | None:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return None
    return f"{s // 60}:{s % 60:02d}"
