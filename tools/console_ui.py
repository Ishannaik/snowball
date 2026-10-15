"""Operator console interface.

Served by tools/console.py as ``/``, ``/app.css``, and ``/app.js``.
The page has no external assets and no inline scripts. Dynamic text is
assigned with text nodes. Private profile values render only in the signed-in
interview and contract review.
"""

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Snowball console</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="masthead">
  <div class="shell head-row">
    <div class="brand">
      <h1>Snowball <span>goal console</span></h1>
      <p class="sub">Local control for <code id="db-name">this database</code> · <span id="refreshed">not signed in</span></p>
    </div>
    <div class="btn-row">
      <button id="signin" class="btn" type="button">Sign in</button>
      <button id="refresh" class="btn" type="button">Refresh</button>
      <button id="signout" class="btn" type="button" hidden>Sign out</button>
    </div>
  </div>
</header>
<div class="shell" id="top-banner"></div>
<nav class="tabs" role="tablist" aria-label="Console views">
  <div class="shell tab-row">
    <button role="tab" type="button" id="tab-goal" aria-controls="panel-goal" aria-selected="true">Goal</button>
    <button role="tab" type="button" id="tab-shortlist" aria-controls="panel-shortlist" aria-selected="false" tabindex="-1">Shortlist</button>
    <button role="tab" type="button" id="tab-activity" aria-controls="panel-activity" aria-selected="false" tabindex="-1">Activity</button>
    <button role="tab" type="button" id="tab-evidence" aria-controls="panel-evidence" aria-selected="false" tabindex="-1">Evidence</button>
    <button role="tab" type="button" id="tab-graph" aria-controls="panel-graph" aria-selected="false" tabindex="-1">Graph<span class="count" id="count-graph"></span></button>
  </div>
</nav>
<main id="main" class="shell">
  <noscript><p role="alert">This console needs its local script to sign in and load records. Nothing here is sample data.</p></noscript>
  <section id="panel-goal" role="tabpanel" aria-labelledby="tab-goal" tabindex="-1">
    <div class="panel-head">
      <h2>Goal</h2>
      <p>Interview, contract review, and execution controls. The graph is a separate public map.</p>
    </div>
    <p id="goal-badge" class="stamp"></p>
    <div id="goal-banners"></div>
    <div id="goal-create"></div>
    <div id="interview-body"></div>
    <div id="contract-body"></div>
    <div id="grant-body"></div>
    <div id="control-body"></div>
    <div id="events-body"></div>
  </section>
  <section id="panel-shortlist" role="tabpanel" aria-labelledby="tab-shortlist" tabindex="-1" hidden>
    <div class="panel-head">
      <h2>Shortlist</h2>
      <p>Constraint results for linked programs. Unknown is not met. Conditional is not an award. No row here is sample data.</p>
    </div>
    <div id="short-banners"></div>
    <div id="short-body"></div>
  </section>
  <section id="panel-activity" role="tabpanel" aria-labelledby="tab-activity" tabindex="-1" hidden>
    <div class="panel-head">
      <h2>Activity</h2>
      <p>Leases, frontier decisions, and ledger headroom as the server recorded them.</p>
    </div>
    <div id="activity-banners"></div>
    <div id="activity-body"></div>
  </section>
  <section id="panel-evidence" role="tabpanel" aria-labelledby="tab-evidence" tabindex="-1" hidden>
    <div class="panel-head">
      <h2 id="evidence-heading" tabindex="-1">Evidence</h2>
      <p>Quotes come from stored snapshots. Human notes are labels, not qualification evidence. An undated source is not an open deadline.</p>
    </div>
    <div id="evidence-banners"></div>
    <div id="evidence-body"></div>
  </section>
  <section id="panel-graph" role="tabpanel" aria-labelledby="tab-graph" tabindex="-1" hidden>
    <div class="panel-head">
      <h2>Graph</h2>
      <p>Public topics, pages, and entities already recorded. The layout is deterministic. An empty map is not blocked research, and blocked research does not invent nodes.</p>
    </div>
    <div id="graph-banners"></div>
    <div class="graph-tools">
      <fieldset class="types" id="graph-types"><legend>Show</legend></fieldset>
      <div class="field"><label for="g-q">Find node</label><input type="search" id="g-q" autocomplete="off" placeholder="Name or URL"></div>
      <div class="btn-row" role="group" aria-label="Graph view controls">
        <button class="btn" id="g-zoom-in" type="button" aria-label="Zoom in">+</button>
        <button class="btn" id="g-zoom-out" type="button" aria-label="Zoom out">&minus;</button>
        <button class="btn" id="g-fit" type="button">Fit</button>
        <button class="btn" id="g-clear" type="button">Clear selection</button>
      </div>
    </div>
    <p id="graph-note" class="summary"></p>
    <div class="graph-layout">
      <div class="graph-stage" id="graph-stage">
        <svg id="graph-svg" tabindex="0" role="group" aria-roledescription="graph canvas" aria-label="Knowledge graph" aria-describedby="graph-help"></svg>
        <div id="graph-overlay" class="overlay" hidden></div>
        <p id="graph-msg" class="stage-state" aria-live="polite"></p>
      </div>
      <aside class="graph-side" aria-label="Graph details">
        <section class="side-box" id="graph-detail" aria-live="polite"></section>
        <section class="side-box">
          <h3>Nodes by connections</h3>
          <p id="graph-list-note" class="summary"></p>
          <ul id="graph-list" class="node-list"></ul>
        </section>
      </aside>
    </div>
    <p id="graph-help" class="note">Canvas: drag to pan, scroll to zoom, click a node to inspect it. With the canvas focused, arrow keys pan, + and minus zoom, 0 fits, and Escape clears the selection. The node list and the connection buttons reach every node without a pointer.</p>
  </section>
</main>
<div id="sr-status" class="sr-only" role="status" aria-live="polite"></div>
<div id="login-dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="login-title" hidden tabindex="-1">
  <div class="dialog-card">
    <h2 id="login-title" tabindex="-1">Sign in</h2>
    <p>Enter the one-time token printed by the console token command. It is sent once and is not stored in this browser. Reloading uses the session cookie, not the token.</p>
    <form id="login-form" autocomplete="off">
      <div class="field">
        <label for="login-token">Sign-in token</label>
        <input id="login-token" type="password" autocomplete="off" spellcheck="false" maxlength="200">
      </div>
      <p id="login-error" class="form-error" role="alert"></p>
      <div class="btn-row">
        <button class="btn" type="submit">Sign in</button>
        <button class="btn" id="login-close" type="button">Close</button>
      </div>
    </form>
  </div>
</div>
<div id="review-dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="review-title" hidden tabindex="-1">
  <div class="dialog-card">
    <h2 id="review-title" tabindex="-1">Review contract</h2>
    <p>The hash covers the contract body and the frozen profile snapshot. Public research receives only the public frame. Approving does not start research.</p>
    <p class="mono" id="review-hash"></p>
    <div id="review-body"></div>
    <p id="review-error" class="form-error" role="alert"></p>
    <div class="btn-row">
      <button class="btn" id="review-close" type="button">Close</button>
      <button class="btn" id="review-approve" type="button" disabled>Approve this hash</button>
    </div>
  </div>
</div>
<div id="stale-dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="stale-title" hidden tabindex="-1">
  <div class="dialog-card">
    <h2 id="stale-title" tabindex="-1">The record changed</h2>
    <p id="stale-text"></p>
    <div class="btn-row">
      <button class="btn" id="stale-reload" type="button">Reload current record</button>
      <button class="btn" id="stale-close" type="button">Close</button>
    </div>
  </div>
</div>
<div id="stop-dialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="stop-title" hidden tabindex="-1">
  <div class="dialog-card">
    <h2 id="stop-title" tabindex="-1">Stop the goal</h2>
    <p>Stop revokes the active grant and keeps stored evidence. It does not delete snapshots.</p>
    <div class="field">
      <label for="stop-reason">Reason</label>
      <textarea id="stop-reason" rows="3" maxlength="2000"></textarea>
    </div>
    <p id="stop-error" class="form-error" role="alert"></p>
    <div class="btn-row">
      <button class="btn" id="stop-close" type="button">Close</button>
      <button class="btn" id="stop-confirm" type="button">Stop and revoke the grant</button>
    </div>
  </div>
</div>
<script src="/app.js" defer></script>
</body>
</html>
"""

CSS = r"""
:root{
  --paper:#f5f2ea;--paper-2:#ebe6d8;--card:#fbf9f4;--ink:#1c1a16;--ink-2:#45413a;--muted:#635e53;--rule:#d6cfbd;--edge:#b3aa93;
  --accent:#b23c12;--verified:#0d6457;--focus:#1d4ed8;
  --n-family:#6b4f8f;--n-topic:#2f5f8f;--n-domain:#8a6d2f;--n-source:#5c7d70;--n-hub:#b8681c;--n-entity:#0d6457;
  --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Charter,Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#15140f;--paper-2:#221f18;--card:#1c1a14;--ink:#ece6d6;--ink-2:#cdc6b3;--muted:#a59e8a;--rule:#3b372c;--edge:#5a5444;
  --accent:#f0895a;--verified:#62c6b2;--focus:#9db9ff;
  --n-family:#b79dd8;--n-topic:#82b2e2;--n-domain:#d4b670;--n-source:#9fc4b5;--n-hub:#f0a65c;--n-entity:#62c6b2;
}}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
[hidden]{display:none!important}
.skip{position:absolute;left:-9999px;top:0}
.skip:focus{left:1rem;top:1rem;z-index:20;background:var(--ink);color:var(--paper);padding:.5rem .8rem}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.muted{color:var(--muted)}
code,.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
a{color:var(--verified);text-underline-offset:2px;overflow-wrap:anywhere}
a.ext::after{content:" \2197";font-size:.85em}

.masthead{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-end;justify-content:space-between;padding:1.4rem clamp(1rem,4vw,2.5rem) 1rem;border-bottom:3px double var(--rule)}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(1.6rem,3.2vw,2.2rem);letter-spacing:-.01em;margin:0;line-height:1.1}
h1 span{font-style:italic;font-weight:400;color:var(--ink-2)}
.sub{margin:.3rem 0 0;color:var(--muted);font-size:.875rem}
.btn{font:inherit;font-size:.9rem;padding:.45rem .9rem;border:1px solid var(--ink);background:transparent;color:var(--ink);border-radius:2px;cursor:pointer;min-height:44px;min-width:44px}
.btn:hover{background:var(--ink);color:var(--paper)}
.btn:disabled{opacity:.55;cursor:progress}
.btn-row{display:flex;flex-wrap:wrap;gap:.4rem}

.tabs{display:flex;padding:0 clamp(1rem,4vw,2.5rem);border-bottom:1px solid var(--rule);overflow-x:auto}
.tabs [role=tab]{font:inherit;font-family:var(--serif);font-size:1.08rem;background:none;border:0;border-bottom:3px solid transparent;padding:.8rem 1rem .55rem;color:var(--muted);cursor:pointer;white-space:nowrap;min-height:44px}
.tabs [role=tab]:hover{color:var(--ink)}
.tabs [role=tab][aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.count{font-family:var(--mono);font-size:.75rem;color:var(--muted);margin-left:.4rem}
.count:empty{display:none}

main{padding:1.5rem clamp(1rem,4vw,2.5rem) 3rem;max-width:1440px;margin:0 auto}
[role=tabpanel]:focus{outline:none}
.panel-head h2{font-family:var(--serif);font-size:1.6rem;margin:0 0 .25rem;font-weight:600}
.panel-head p{margin:0 0 1.1rem;color:var(--ink-2);max-width:70ch}
.summary{font-family:var(--mono);font-size:.8rem;color:var(--muted);margin:.25rem 0 1rem}
.summary:empty{display:none}
.note{font-size:.82rem;color:var(--muted);margin:.75rem 0 0;max-width:80ch}

.filters{display:grid;grid-template-columns:minmax(14rem,2fr) repeat(4,minmax(9rem,1fr));gap:.75rem;align-items:end;margin-bottom:.5rem}
.field{display:flex;flex-direction:column;gap:.25rem;min-width:0}
.field label,fieldset.types legend{font-size:.85rem;color:var(--muted)}
input[type=search],select{font:inherit;font-size:.95rem;padding:.5rem .65rem;border:1px solid var(--rule);background:var(--card);color:var(--ink);border-radius:2px;min-height:44px;width:100%}
input[type=search]:focus,select:focus{border-color:var(--ink)}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,23rem),1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--rule);border-top:3px solid var(--ink-2);padding:1rem 1.15rem;display:flex;flex-direction:column;gap:.65rem;min-width:0}
.card.soon{border-top-color:var(--accent)}
.card.past{border-top-style:dashed}
.meta-line{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem .8rem;font-size:.85rem;color:var(--muted);margin:0}
.tag{border:1px solid var(--rule);padding:0 .35rem;border-radius:2px}
.card h3{font-family:var(--serif);font-size:1.25rem;line-height:1.25;margin:0;font-weight:600;overflow-wrap:anywhere}
.org{margin:-.4rem 0 0;color:var(--ink-2)}
dl.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.2rem .9rem;margin:0;font-size:.92rem}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0;overflow-wrap:anywhere}
.stamp{font-family:var(--mono);font-size:.78rem;padding:0 .35rem;border:1px solid currentColor;border-radius:2px;margin-left:.45rem;white-space:nowrap}
.stamp.soon{color:var(--accent);font-weight:600}
.stamp.past{color:var(--muted)}
.evidence{margin:0;padding:.6rem .85rem;border-left:3px solid var(--verified);background:var(--paper-2)}
.evidence .label{display:block;font-size:.82rem;color:var(--verified);font-weight:700;margin-bottom:.2rem}
.evidence blockquote{margin:0;font-family:var(--serif);font-size:1rem;line-height:1.5;overflow-wrap:anywhere}
.evidence blockquote::before{content:"\201C"}
.evidence blockquote::after{content:"\201D"}
.evidence figcaption{margin-top:.4rem;font-size:.82rem;color:var(--ink-2)}
details.changes summary{cursor:pointer;font-size:.88rem;color:var(--ink-2);min-height:32px;padding:.2rem 0}
details.changes ol{margin:.3rem 0 0;padding-left:1.2rem;font-size:.86rem;display:flex;flex-direction:column;gap:.45rem}
blockquote.mini{margin:.2rem 0 0;padding-left:.6rem;border-left:2px solid var(--rule);font-family:var(--serif);color:var(--ink-2);overflow-wrap:anywhere}
.seen{margin:auto 0 0;font-size:.78rem;color:var(--muted);font-family:var(--mono)}

.state{padding:2rem 1.25rem;text-align:center;color:var(--ink-2);border:1px dashed var(--rule);background:var(--card);grid-column:1/-1}
.state h3{font-family:var(--serif);font-size:1.15rem;margin:0 0 .35rem;font-weight:600}
.state p{margin:0 auto .75rem;max-width:60ch}
.state.error{border:1px solid var(--accent);color:var(--ink)}
.state.error h3{color:var(--accent)}
.loading::before{content:"";display:inline-block;width:.85rem;height:.85rem;border:2px solid var(--rule);border-top-color:var(--ink);border-radius:50%;margin-right:.55rem;vertical-align:-1px;animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.graph-tools{display:flex;flex-wrap:wrap;gap:.8rem 1.4rem;align-items:flex-end;margin-bottom:.5rem}
fieldset.types{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:.2rem .9rem;min-width:0}
fieldset.types legend{padding:0;margin-bottom:.15rem}
.chk{display:inline-flex;align-items:center;gap:.4rem;font-size:.9rem;min-height:36px;cursor:pointer}
.chk input{width:1.05rem;height:1.05rem;accent-color:var(--ink)}
.swatch{width:.75rem;height:.75rem;border-radius:50%;display:inline-block;flex:none}
.sw-family{background:var(--n-family)}.sw-topic{background:var(--n-topic)}.sw-domain{background:var(--n-domain)}
.sw-source{background:var(--n-source)}.sw-hub{background:var(--n-hub)}.sw-entity{background:var(--n-entity)}
.graph-layout{display:grid;grid-template-columns:minmax(0,1fr) 21rem;gap:1rem}
.graph-stage{position:relative;border:1px solid var(--rule);background:var(--card);height:min(72vh,680px);min-height:360px;overflow:hidden}
#graph-svg{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
#graph-svg.panning{cursor:grabbing}
.overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:1rem;background:var(--card)}
.overlay .state{max-width:34rem}
.stage-state{position:absolute;left:0;right:0;bottom:0;margin:0;padding:.45rem .75rem;font-family:var(--mono);font-size:.8rem;color:var(--ink-2);background:var(--card);border-top:1px solid var(--rule)}
.stage-state:empty{display:none}
svg .edge{stroke:var(--edge);stroke-width:1;vector-effect:non-scaling-stroke}
svg .edge.l-evidences,svg .edge.l-corroborates,svg .edge.l-lists{stroke:var(--verified);stroke-opacity:.5}
svg .node circle{stroke:var(--card);stroke-width:1.5;cursor:pointer;vector-effect:non-scaling-stroke}
svg .t-family circle{fill:var(--n-family)}svg .t-topic circle{fill:var(--n-topic)}svg .t-domain circle{fill:var(--n-domain)}
svg .t-source circle{fill:var(--n-source)}svg .t-hub circle{fill:var(--n-hub)}svg .t-entity circle{fill:var(--n-entity)}
svg .node text{font:11px var(--sans);fill:var(--ink);paint-order:stroke;stroke:var(--card);stroke-width:3px;stroke-linejoin:round;pointer-events:none;display:none}
svg .node.lbl text,svg.zoomed .node text,svg .node.sel text,svg .node.nb text,svg .node.match text{display:inline}
svg.searching .node:not(.match){opacity:.18}
svg.has-sel .node:not(.sel):not(.nb){opacity:.16}
svg.has-sel .edge:not(.hot){opacity:.1}
svg .edge.hot{stroke:var(--ink);stroke-opacity:.85;stroke-width:1.6}
svg .node.sel circle{stroke:var(--accent);stroke-width:3}
svg .node.sel text{font-weight:700}
svg .hide{display:none}
.graph-side{display:flex;flex-direction:column;gap:1rem;min-width:0}
.side-box{border:1px solid var(--rule);background:var(--card);padding:.9rem 1rem;min-width:0}
.side-box h3{font-family:var(--serif);font-size:1.12rem;margin:.1rem 0 .5rem;overflow-wrap:anywhere}
.side-box h3:focus{outline:none}
.side-box h4{font-size:.88rem;color:var(--muted);margin:1rem 0 .3rem;font-weight:600}
.side-box .evidence{margin-top:.6rem}
.side-box .summary{margin:0 0 .5rem}
.node-list,.nb-list{list-style:none;margin:0;padding:0}
.node-list{max-height:24rem;overflow:auto}
.nb-list{max-height:18rem;overflow:auto}
.node-list button,.nb-list button{font:inherit;font-size:.88rem;text-align:left;width:100%;background:none;border:0;border-bottom:1px solid var(--rule);padding:.4rem .3rem;color:var(--ink);cursor:pointer;display:flex;gap:.5rem;align-items:center;min-height:40px}
.node-list button:hover,.nb-list button:hover{background:var(--paper-2)}
.node-list button[aria-pressed=true]{background:var(--paper-2);font-weight:600}
.node-list .nm,.nb-list .nm{min-width:0;overflow-wrap:anywhere}
.node-list .deg{margin-left:auto;font-family:var(--mono);font-size:.75rem;color:var(--muted)}
.nb-list .rel{font-family:var(--mono);font-size:.72rem;color:var(--muted);white-space:nowrap}

.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,12rem),1fr));gap:.75rem;margin-bottom:1.5rem}
.tile{border:1px solid var(--rule);border-top:3px solid var(--ink-2);background:var(--card);padding:.75rem 1rem}
.tile .k{font-size:.85rem;color:var(--muted)}
.tile .v{font-family:var(--mono);font-size:1.65rem;font-variant-numeric:tabular-nums;line-height:1.25}
.tile .n{font-size:.8rem;color:var(--ink-2);overflow-wrap:anywhere}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,27rem),1fr));gap:1.25rem;align-items:start}
.tbl-wrap{overflow-x:auto;border:1px solid var(--rule);background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:.88rem}
caption{text-align:left;font-family:var(--serif);font-size:1.1rem;font-weight:600;padding:.75rem .9rem .35rem}
th,td{padding:.45rem .9rem;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{font-size:.85rem;color:var(--muted);font-weight:600}
td{overflow-wrap:anywhere}
td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl-wrap .note{padding:0 .9rem .75rem;margin:.5rem 0 0}
.bar{height:.55rem;background:var(--paper-2);min-width:5rem;position:relative;margin-top:.45rem}
.bar>span{position:absolute;left:0;top:0;bottom:0;background:var(--ink-2)}
.foot{margin-top:1.25rem}

@media (max-width:960px){
  .graph-layout{grid-template-columns:1fr}
  .graph-stage{height:60vh}
  .filters{grid-template-columns:1fr 1fr}
  .filters .field.q{grid-column:1/-1}
}
@media (max-width:540px){
  .filters{grid-template-columns:1fr}
  .masthead{align-items:flex-start}
  .tabs [role=tab]{padding:.75rem .7rem .5rem;font-size:1rem}
}
@media (prefers-reduced-motion:reduce){.loading::before{animation:none}}
:root{
  --met:var(--verified);
  --unmet:var(--accent);
  --unknown:var(--muted);
  --conditional:var(--n-hub);
  --s1:8px;--s2:16px;--s3:24px;--s4:32px;--s5:40px;--s6:48px;
}
.shell{max-width:1200px;margin:0 auto;width:100%}
.masthead{display:block;padding:0}
.head-row{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:flex-end;justify-content:space-between;padding:var(--s3) var(--s3) var(--s2)}
.tabs{display:block;padding:0;max-width:none}
.tab-row{display:flex;overflow-x:auto}
main.shell{padding:var(--s3) var(--s3) var(--s6);max-width:1200px}
h2,h3,h4{font-family:var(--serif)}
.stack{display:flex;flex-direction:column;gap:var(--s2)}
.inline{display:flex;flex-wrap:wrap;gap:var(--s1);align-items:end}
.banner{border:1px solid var(--rule);border-left:8px solid var(--ink-2);background:var(--card);padding:var(--s2);margin:0 0 var(--s2)}
.banner h3{font-size:1.15rem;margin:0 0 var(--s1)}
.banner p{margin:0;max-width:70ch}
.banner.bad{border-left-color:var(--unmet)}
.banner.wait{border-left-color:var(--conditional)}
.banner.ok{border-left-color:var(--met)}
.banner.info{border-left-color:var(--n-topic)}
.form-error{color:var(--unmet);min-height:1.25em;margin:var(--s1) 0}
.form-error:empty{display:none}
textarea,input[type=text],input[type=password],input[type=datetime-local]{
  font:inherit;font-size:.95rem;padding:8px;border:1px solid var(--rule);background:var(--card);
  color:var(--ink);border-radius:2px;min-height:44px;width:100%}
textarea{min-height:96px;resize:vertical}
.choices{display:flex;flex-wrap:wrap;gap:var(--s1) var(--s2)}
.dialog{position:fixed;inset:0;z-index:30;display:flex;align-items:flex-start;justify-content:center;padding:var(--s2);
  background:rgba(28,26,22,.45);overflow:auto}
.dialog-card{background:var(--card);color:var(--ink);border:1px solid var(--rule);width:min(720px,100%);
  margin:var(--s4) auto;padding:var(--s3);max-height:none}
.dialog-card h2{margin-top:0}
#review-hash{overflow-wrap:anywhere}
.program{border:1px solid var(--rule);background:var(--card);padding:var(--s2);margin:0 0 var(--s2);min-width:0}
.cells{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:var(--s1)}
.cell{font:inherit;text-align:left;background:var(--card);color:var(--ink);border:1px solid var(--rule);
  border-radius:2px;min-height:44px;padding:8px;cursor:pointer;width:100%}
.cell-wrap{display:flex;flex-direction:column;gap:4px;min-width:0}
.cell-key,.cell-note{font-size:.82rem;color:var(--muted);overflow-wrap:anywhere}
.mark.met{border-color:var(--met);color:var(--met)}
.mark.unmet{border-color:var(--unmet);color:var(--unmet)}
.mark.unknown{color:var(--unknown)}
.mark.conditional{border-color:var(--conditional)}
.mark.contested{border-color:var(--ink);background-image:repeating-linear-gradient(-45deg,transparent,transparent 6px,var(--rule) 6px,var(--rule) 8px)}
.mark.other{border-style:dashed}
pre.mono{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
.snap{border-top:1px solid var(--rule);padding-top:var(--s1)}
.gauge{margin:0 0 var(--s2)}
.gauge .bar{height:8px}
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s2)}
@media (max-width:1200px){
  .shell,main.shell{max-width:1200px}
}
@media (max-width:720px){
  .head-row{align-items:flex-start;padding:var(--s2)}
  .split,.graph-layout,.grid2,.cells{grid-template-columns:1fr}
  .graph-stage{height:60vh}
  .filters{grid-template-columns:1fr}
  .dialog{padding:0}
  .dialog-card{margin:0;min-height:100%}
  main.shell{padding:var(--s2) var(--s2) var(--s5)}
}
@media (prefers-reduced-motion:reduce){
  .loading::before{animation:none}
  html{scroll-behavior:auto}
}
.btn,.tabs button,.cell{min-height:44px}
.chk{min-height:44px;display:inline-flex;align-items:center;gap:8px}


"""

JS = r"""'use strict';
(() => {
  const TYPES = [
    ['entity', 'Opportunities'], ['source', 'Source pages'], ['hub', 'Hub pages'],
    ['topic', 'Topics'], ['domain', 'Domains'], ['family', 'Families'],
  ];
  const TYPE_LABEL = Object.fromEntries(TYPES);

  const state = {
    csrf: '', sessionExpires: null, goal: null, interview: null, events: null, shortlist: null,
    activity: null, evidence: null, status: null, g: null, loading: false,
    review: null, forcedKey: '', evidenceKey: '', evidenceField: '', stale: false,
  };
  const prefs = { hidden: new Set(), q: '' };
  let graphToken = 0;

  const $ = (id) => document.getElementById(id);

  // Optional children are dropped (null/undefined/booleans), but 0 and '' are kept on purpose.
  function kidNodes(kids) {
    return kids.flat(Infinity)
      .filter((c) => c !== null && c !== undefined && typeof c !== 'boolean')
      .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
  }
  // All DOM is built here; strings always become text nodes, never markup.
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    n.append(...kidNodes(kids));
    return n;
  }
  function svgEl(tag, attrs) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }
  const truncate = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const plural = (n, word) => `${fmtNum(n)} ${word}${Number(n) === 1 ? '' : 's'}`;
  const safeClass = (s) => String(s || '').toLowerCase().replace(/[^a-z]/g, '');
  function fmtNum(n) { return n === null || n === undefined || n === '' || isNaN(Number(n)) ? '—' : Number(n).toLocaleString(); }
  function fmtTime(sec) {
    if (!sec) return '—';
    const d = new Date(Number(sec) * 1000);
    return isNaN(d.getTime()) ? '—' : d.toLocaleString();
  }
  function fmtUsd(micro) { return micro === null || micro === undefined ? '—' : '$' + (Number(micro) / 1e6).toFixed(2); }
  function announce(msg) { $('sr-status').textContent = msg; }

  function safeUrl(u) {
    if (typeof u !== 'string' || !u) return null;
    try {
      const x = new URL(u);
      return (x.protocol === 'http:' || x.protocol === 'https:') ? x.href : null;
    } catch (_) { return null; }
  }
  function linkTo(u, label) {
    if (!u) return el('span', { class: 'muted' }, 'none recorded');
    const href = safeUrl(u);
    if (!href) return el('span', { class: 'muted' }, 'withheld: not an http(s) link');
    let text = label;
    if (!text) { try { const x = new URL(href); text = x.host + (x.pathname.length > 1 ? truncate(x.pathname, 40) : ''); } catch (_) { text = href; } }
    return el('a', { href, class: 'ext', target: '_blank', rel: 'noopener noreferrer nofollow' }, text);
  }
  function stateBox(kind, title, detail, action, actionLabel) {
    return el('div', { class: 'state ' + kind, role: kind === 'error' ? 'alert' : 'status' },
      el('h3', { class: kind === 'loading' ? 'loading' : null }, title),
      detail ? el('p', null, detail) : null,
      action ? el('button', { class: 'btn', type: 'button', onclick: action }, actionLabel || 'Try again') : null);
  }


  /* ---------- knowledge graph ---------- */
  function setOverlay(node) { const o = $('graph-overlay'); if (node) { o.replaceChildren(node); o.hidden = false; } else { o.replaceChildren(); o.hidden = true; } }
  function setGraphMsg(text) { $('graph-msg').textContent = text; }

  function initGraph(data) {
    const token = ++graphToken;
    const nodes = data.nodes.filter((n) => n && typeof n.id === 'string')
      .map((n, i) => ({ ...n, i, type: safeClass(n.type), name: String(n.name ?? n.id), deg: 0, vis: true, match: false, elm: null, btn: null }));
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const edges = [];
    const adj = new Map(nodes.map((n) => [n.i, []]));
    for (const l of data.links || []) {
      const a = byId.get(l.source), b = byId.get(l.target);
      if (!a || !b || a === b) continue;
      const e = { s: a.i, t: b.i, label: String(l.label || 'related'), cls: safeClass(l.label), elm: null };
      edges.push(e);
      a.deg++; b.deg++;
      adj.get(a.i).push({ edge: e, other: b.i, out: true });
      adj.get(b.i).push({ edge: e, other: a.i, out: false });
    }
    state.g = { nodes, edges, adj, sel: null, view: null, tx: 0, ty: 0, k: 1, w: 0, h: 0, needsFit: true, dragMoved: false };
    $('count-graph').textContent = String(nodes.length);
    $('graph-svg').replaceChildren();
    $('graph-svg').classList.remove('has-sel', 'searching', 'zoomed');

    const cut = Object.entries(data.truncated || {});
    const limits = data.limits || {};
    $('graph-note').textContent = cut.length
      ? 'Capped for readability: ' + cut.map(([k, total]) => `${fmtNum(limits[k])} of ${fmtNum(total)} ${k}`).join(', ') + '. Opportunities are kept in order of relevance.'
      : `${plural(nodes.length, 'node')}, ${plural(edges.length, 'connection')}.`;

    renderTypeFilters();
    applyVisibility();
    renderNodeList();
    renderDetail(null);

    if (!nodes.length) {
      setGraphMsg('');
      setOverlay(stateBox('empty', 'The knowledge graph is empty', 'No public topics, source pages or verified entities are recorded in this database yet.'));
      return;
    }
    setOverlay(null);
    setGraphMsg(`Laying out ${plural(nodes.length, 'node')}…`);
    layout(state.g, token, () => {
      drawGraph();
      setGraphMsg('');
      fitView();
    });
  }

  // Deterministic force layout: phyllotaxis seed, pairwise repulsion, degree-weighted springs, weak gravity.
  function layout(g, token, done) {
    const nodes = g.nodes, n = nodes.length;
    const x = new Float64Array(n), y = new Float64Array(n), vx = new Float64Array(n), vy = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const r = 14 * Math.sqrt(i + 0.5), a = i * 2.399963229728653;
      x[i] = r * Math.cos(a); y[i] = r * Math.sin(a);
    }
    const ITER = 300;
    const perFrame = Math.max(1, Math.floor(1.5e6 / Math.max(1, n * n)));
    let it = 0, alpha = 1;
    function tick() {
      for (let i = 0; i < n; i++) {
        const xi = x[i], yi = y[i];
        for (let j = i + 1; j < n; j++) {
          let dx = xi - x[j], dy = yi - y[j];
          let d2 = dx * dx + dy * dy;
          if (d2 > 360000) continue;
          if (d2 < 0.01) { dx = ((i % 7) - 3) * 0.1 + 0.05; dy = ((j % 5) - 2) * 0.1 + 0.05; d2 = dx * dx + dy * dy; }
          const f = 700 * alpha / d2;
          vx[i] += dx * f; vy[i] += dy * f; vx[j] -= dx * f; vy[j] -= dy * f;
        }
      }
      for (const e of g.edges) {
        const a = e.s, b = e.t, da = nodes[a].deg, db = nodes[b].deg;
        const dx = x[b] - x[a], dy = y[b] - y[a];
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (d - 55) / d * alpha * 0.6 / Math.min(da, db);
        const bias = da / (da + db);
        vx[b] -= dx * f * bias; vy[b] -= dy * f * bias;
        vx[a] += dx * f * (1 - bias); vy[a] += dy * f * (1 - bias);
      }
      for (let i = 0; i < n; i++) {
        vx[i] = (vx[i] - x[i] * 0.006 * alpha) * 0.6;
        vy[i] = (vy[i] - y[i] * 0.006 * alpha) * 0.6;
        const sp = Math.hypot(vx[i], vy[i]);
        if (sp > 40) { vx[i] *= 40 / sp; vy[i] *= 40 / sp; }
        x[i] += vx[i]; y[i] += vy[i];
      }
      alpha = Math.max(0.03, alpha * 0.985);
    }
    function frame() {
      if (token !== graphToken) return; // a newer refresh superseded this layout
      for (let s = 0; s < perFrame && it < ITER; s++, it++) tick();
      if (it < ITER) {
        setGraphMsg(`Laying out ${plural(n, 'node')}… ${Math.round((it / ITER) * 100)}%`);
        requestAnimationFrame(frame);
        return;
      }
      for (let i = 0; i < n; i++) { nodes[i].x = x[i]; nodes[i].y = y[i]; }
      done();
    }
    requestAnimationFrame(frame);
  }

  function drawGraph() {
    const g = state.g;
    const svg = $('graph-svg');
    const view = svgEl('g');
    const edgeLayer = svgEl('g'), nodeLayer = svgEl('g');
    for (const e of g.edges) {
      const a = g.nodes[e.s], b = g.nodes[e.t];
      e.elm = svgEl('line', { class: 'edge l-' + e.cls, x1: a.x.toFixed(1), y1: a.y.toFixed(1), x2: b.x.toFixed(1), y2: b.y.toFixed(1) });
      edgeLayer.append(e.elm);
    }
    const labelled = new Set(g.nodes.slice().sort((p, q) => q.deg - p.deg).slice(0, 24).map((n) => n.i));
    for (const n of g.nodes) {
      const r = Math.min(16, 4 + Math.sqrt(n.deg) * 1.8);
      const grp = svgEl('g', { class: 'node t-' + n.type + (labelled.has(n.i) ? ' lbl' : ''), transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})` });
      const title = svgEl('title');
      title.textContent = `${TYPE_LABEL[n.type] || n.type}: ${n.name}`;
      const text = svgEl('text', { x: (r + 3).toFixed(1), y: 4 });
      text.textContent = truncate(n.name, 42);
      grp.append(title, svgEl('circle', { r: r.toFixed(1) }), text);
      grp.addEventListener('click', (ev) => { ev.stopPropagation(); if (!g.dragMoved) selectNode(n.i, false); });
      n.elm = grp;
      nodeLayer.append(grp);
    }
    view.append(edgeLayer, nodeLayer);
    svg.replaceChildren(view);
    g.view = view;
    applyVisibility();
    applyTransform();
  }

  function applyTransform() {
    const g = state.g;
    if (!g || !g.view) return;
    g.view.setAttribute('transform', `translate(${g.tx.toFixed(1)},${g.ty.toFixed(1)}) scale(${g.k.toFixed(4)})`);
    $('graph-svg').classList.toggle('zoomed', g.k >= 1.8);
  }
  function resizeStage() {
    const r = $('graph-svg').getBoundingClientRect();
    if (state.g) { state.g.w = r.width; state.g.h = r.height; }
  }
  function fitView() {
    const g = state.g;
    if (!g || !g.view) return;
    resizeStage();
    if (!g.w || !g.h) { g.needsFit = true; return; }
    const pts = g.nodes.filter((n) => n.vis);
    if (!pts.length) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of pts) { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); }
    const bw = Math.max(x1 - x0, 80), bh = Math.max(y1 - y0, 80);
    g.k = Math.min(3, Math.max(0.05, 0.88 * Math.min(g.w / bw, g.h / bh)));
    g.tx = g.w / 2 - ((x0 + x1) / 2) * g.k;
    g.ty = g.h / 2 - ((y0 + y1) / 2) * g.k;
    g.needsFit = false;
    applyTransform();
  }
  function zoomAt(factor, cx, cy) {
    const g = state.g;
    if (!g || !g.view) return;
    const k = Math.min(8, Math.max(0.05, g.k * factor));
    g.tx = cx - (cx - g.tx) * (k / g.k);
    g.ty = cy - (cy - g.ty) * (k / g.k);
    g.k = k;
    applyTransform();
  }
  function centerOn(n) {
    const g = state.g;
    if (!g || !g.view || typeof n.x !== 'number') return;
    resizeStage();
    if (!g.w || !g.h) return;
    g.tx = g.w / 2 - n.x * g.k;
    g.ty = g.h / 2 - n.y * g.k;
    applyTransform();
  }

  function renderTypeFilters() {
    const g = state.g;
    const counts = {};
    for (const n of g.nodes) counts[n.type] = (counts[n.type] || 0) + 1;
    const box = $('graph-types');
    box.replaceChildren(el('legend', null, 'Show'), ...TYPES.filter(([t]) => counts[t]).map(([t, label]) => {
      const cb = el('input', { type: 'checkbox' });
      cb.checked = !prefs.hidden.has(t);
      cb.addEventListener('change', () => {
        if (cb.checked) prefs.hidden.delete(t); else prefs.hidden.add(t);
        applyVisibility();
        renderNodeList();
      });
      return el('label', { class: 'chk' }, cb, el('span', { class: 'swatch sw-' + t, 'aria-hidden': 'true' }), label, el('span', { class: 'count' }, String(counts[t])));
    }));
  }
  function applyVisibility() {
    const g = state.g;
    if (!g) return;
    const q = prefs.q;
    for (const n of g.nodes) {
      n.vis = !prefs.hidden.has(n.type);
      n.match = !!q && (n.name.toLowerCase().includes(q) || (typeof n.url === 'string' && n.url.toLowerCase().includes(q)));
      if (n.elm) { n.elm.classList.toggle('hide', !n.vis); n.elm.classList.toggle('match', n.match); }
    }
    for (const e of g.edges) if (e.elm) e.elm.classList.toggle('hide', !(g.nodes[e.s].vis && g.nodes[e.t].vis));
    $('graph-svg').classList.toggle('searching', !!q);
    if (g.sel !== null && !g.nodes[g.sel].vis) selectNode(null);
  }
  function renderNodeList() {
    const g = state.g;
    if (!g) return;
    for (const n of g.nodes) n.btn = null;
    const items = g.nodes.filter((n) => n.vis && (!prefs.q || n.match))
      .sort((a, b) => b.deg - a.deg || a.name.localeCompare(b.name));
    $('graph-list-note').textContent = items.length > LIST_LIMIT
      ? `Showing the ${LIST_LIMIT} most connected of ${fmtNum(items.length)}. Search to narrow.`
      : (items.length ? plural(items.length, 'node') : 'No nodes match the current filters.');
    $('graph-list').replaceChildren(...items.slice(0, LIST_LIMIT).map((n) => {
      n.btn = el('button', { type: 'button', 'aria-pressed': g.sel === n.i ? 'true' : 'false', onclick: () => selectNode(n.i, true) },
        el('span', { class: 'swatch sw-' + n.type, 'aria-hidden': 'true' }),
        el('span', { class: 'nm' }, truncate(n.name, 70), el('span', { class: 'sr-only' }, `, ${TYPE_LABEL[n.type] || n.type}`)),
        el('span', { class: 'deg', 'aria-label': plural(n.deg, 'connection') }, String(n.deg)));
      return el('li', null, n.btn);
    }));
  }
  function selectNode(i, center) {
    const g = state.g;
    if (!g) return;
    for (const n of g.nodes) {
      if (n.elm) n.elm.classList.remove('sel', 'nb');
      if (n.btn) n.btn.setAttribute('aria-pressed', 'false');
    }
    for (const e of g.edges) if (e.elm) e.elm.classList.remove('hot');
    g.sel = (i === null || i === undefined) ? null : i;
    $('graph-svg').classList.toggle('has-sel', g.sel !== null);
    if (g.sel !== null) {
      const n = g.nodes[g.sel];
      if (n.btn) n.btn.setAttribute('aria-pressed', 'true');
      if (n.elm) { n.elm.classList.add('sel'); n.elm.parentNode.append(n.elm); }
      for (const a of g.adj.get(g.sel) || []) {
        const o = g.nodes[a.other];
        if (o.elm) o.elm.classList.add('nb');
        if (a.edge.elm) a.edge.elm.classList.add('hot');
      }
      if (center) centerOn(n);
    }
    renderDetail(g.sel === null ? null : g.nodes[g.sel]);
  }
  function renderDetail(n) {
    const box = $('graph-detail');
    const hadFocus = box.contains(document.activeElement);
    if (!n) {
      box.replaceChildren(el('h3', null, 'Selection'),
        el('p', { class: 'muted' }, 'Select a node in the canvas or the list below. Its evidence, link and connections appear here; each connection is a button, so the graph can be walked from the keyboard.'));
      return;
    }
    const g = state.g;
    const facts = el('dl', { class: 'facts' });
    for (const pair of Array.isArray(n.facts) ? n.facts : []) {
      if (Array.isArray(pair) && pair.length === 2) facts.append(el('dt', null, String(pair[0])), el('dd', null, String(pair[1])));
    }
    const conns = (g.adj.get(n.i) || []).slice().sort((a, b) => {
      const oa = g.nodes[a.other], ob = g.nodes[b.other];
      return oa.type.localeCompare(ob.type) || ob.deg - oa.deg || oa.name.localeCompare(ob.name);
    });
    const heading = el('h3', { tabindex: '-1' }, n.name);
    box.replaceChildren(...kidNodes([
      el('p', { class: 'meta-line' }, el('span', { class: 'swatch sw-' + n.type, 'aria-hidden': 'true' }), TYPE_LABEL[n.type] || n.type),
      heading,
      facts.childNodes.length ? facts : null,
      typeof n.quote === 'string' && n.quote ? el('figure', { class: 'evidence' }, el('span', { class: 'label' }, 'Verified quote'), el('blockquote', null, n.quote)) : null,
      n.url ? el('p', null, linkTo(n.url)) : null,
      el('h4', null, `Connections (${fmtNum(conns.length)})`),
      conns.length
        ? el('ul', { class: 'nb-list' }, ...conns.slice(0, 80).map((a) => {
            const o = g.nodes[a.other];
            return el('li', null, el('button', { type: 'button', onclick: () => selectNode(a.other, true) },
              el('span', { class: 'swatch sw-' + o.type, 'aria-hidden': 'true' }),
              el('span', { class: 'rel' }, a.out ? `${a.edge.label} →` : `← ${a.edge.label}`),
              el('span', { class: 'nm' }, truncate(o.name, 60), el('span', { class: 'sr-only' }, `, ${TYPE_LABEL[o.type] || o.type}`))));
          }))
        : el('p', { class: 'muted' }, 'No connections on this map.'),
      conns.length > 80 ? el('p', { class: 'summary' }, `First 80 of ${fmtNum(conns.length)} shown.`) : null]));
    if (hadFocus) heading.focus();
  }

  function initGraphInteractions() {
    const svg = $('graph-svg');
    let drag = null;
    svg.addEventListener('pointerdown', (ev) => {
      if (!state.g || !state.g.view || ev.button !== 0) return;
      drag = { x: ev.clientX, y: ev.clientY, tx: state.g.tx, ty: state.g.ty, id: ev.pointerId };
      state.g.dragMoved = false;
    });
    svg.addEventListener('pointermove', (ev) => {
      const g = state.g;
      if (!drag || !g || ev.pointerId !== drag.id) return;
      const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      if (!g.dragMoved) {
        if (Math.hypot(dx, dy) < 4) return;
        g.dragMoved = true;
        svg.setPointerCapture(ev.pointerId);
        svg.classList.add('panning');
      }
      g.tx = drag.tx + dx; g.ty = drag.ty + dy;
      applyTransform();
    });
    const end = (ev) => {
      if (!drag) return;
      drag = null;
      svg.classList.remove('panning');
      if (svg.hasPointerCapture(ev.pointerId)) svg.releasePointerCapture(ev.pointerId);
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);
    svg.addEventListener('click', (ev) => { if (state.g && !state.g.dragMoved && ev.target === svg) selectNode(null); });
    svg.addEventListener('wheel', (ev) => {
      if (!state.g || !state.g.view) return;
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      const dy = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
      zoomAt(Math.exp(-dy * 0.0015), ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    svg.addEventListener('keydown', (ev) => {
      const g = state.g;
      if (!g || !g.view) return;
      resizeStage();
      const step = 60;
      switch (ev.key) {
        case 'ArrowLeft': g.tx += step; break;
        case 'ArrowRight': g.tx -= step; break;
        case 'ArrowUp': g.ty += step; break;
        case 'ArrowDown': g.ty -= step; break;
        case '+': case '=': zoomAt(1.25, g.w / 2, g.h / 2); break;
        case '-': case '_': zoomAt(0.8, g.w / 2, g.h / 2); break;
        case '0': fitView(); break;
        case 'Escape': selectNode(null); break;
        default: return;
      }
      ev.preventDefault();
      applyTransform();
    });
    $('g-zoom-in').addEventListener('click', () => { if (state.g) { resizeStage(); zoomAt(1.25, state.g.w / 2, state.g.h / 2); } });
    $('g-zoom-out').addEventListener('click', () => { if (state.g) { resizeStage(); zoomAt(0.8, state.g.w / 2, state.g.h / 2); } });
    $('g-fit').addEventListener('click', fitView);
    $('g-clear').addEventListener('click', () => selectNode(null));
    let t = null;
    $('g-q').addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => { prefs.q = $('g-q').value.trim().toLowerCase(); applyVisibility(); renderNodeList(); }, 150);
    });
    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(() => { resizeStage(); if (state.g && state.g.needsFit) fitView(); }).observe($('graph-stage'));
    }
  }


const BANK = [{"field_key": "degree_level", "text": "What degree level should this goal pursue? Answer masters, phd, or other. The profile name does not choose a degree for you.", "answer_type": "degree_level", "required": true, "visibility_default": "public_ok"}, {"field_key": "modality", "text": "What study modality should this goal pursue? Answer on_campus, online, or hybrid.", "answer_type": "modality", "required": true, "visibility_default": "public_ok"}, {"field_key": "field_tags", "text": "Which field tags should public search use? Answer a JSON list of short labels. No field list is assumed.", "answer_type": "string_list", "required": true, "visibility_default": "public_ok"}, {"field_key": "regions", "text": "Which regions should public search use? Answer a JSON list of region labels. An empty list does not mean worldwide.", "answer_type": "region_list", "required": true, "visibility_default": "public_ok"}, {"field_key": "intake_windows", "text": "Which intake windows should public search use? Answer a JSON list of YYYY-season values, with season fall, spring, summer, or winter. No year is assumed.", "answer_type": "intake_list", "required": true, "visibility_default": "public_ok"}, {"field_key": "scope_broad_acknowledged", "text": "Regions are empty. Do you explicitly acknowledge a bounded broad public-research scope while personal eligibility stays unknown? Answer yes or no. No empty region list is broadened automatically.", "answer_type": "bool", "required": true, "visibility_default": "public_ok"}, {"field_key": "required_components", "text": "Which funding components are required? Answer a JSON object with an explicit true or false for tuition, mandatory_fees, living_amount, living_duration, health_insurance, visa_costs, application_costs, travel_costs, deposits, and proof_of_funds_liquidity. None of these is preselected, and they are not all additive expenses.", "answer_type": "components", "required": true, "visibility_default": "public_ok"}, {"field_key": "grants_only", "text": "Must every required component be satisfied without loans? Answer yes or no.", "answer_type": "bool", "required": true, "visibility_default": "public_ok"}, {"field_key": "completion_min_candidates", "text": "How many matching opportunities are required before a positive shortlist? Answer a positive whole number. Rejected programs do not count, and no minimum is assumed.", "answer_type": "positive_int", "required": true, "visibility_default": "public_ok"}, {"field_key": "execution_policy", "text": "What finite execution policy should be approved? Answer JSON with lifetime_limits for queued_nodes, visited_nodes, fetches, queries, and model_tokens; max_depth; max_queue; per_domain_inflight; max_hypotheses_per_wave; exploration_share from 0 to 1; and usage_mode estimated or hard. Every ceiling is a finite integer. Hard mode does not claim an attested transport.", "answer_type": "execution_policy", "required": true, "visibility_default": "public_ok"}, {"field_key": "monitoring", "text": "What refresh work may a future monitoring grant perform? Answer JSON with permitted_work drawn from recheck_official_sources, refresh_cycle_dates, and refresh_public_funding_facts, plus max_refreshes and interval_days. This describes permitted work and does not start monitoring.", "answer_type": "monitoring", "required": true, "visibility_default": "public_ok"}, {"field_key": "consent_private_evaluation", "text": "Do you consent to private eligibility evaluation on this machine? Answer yes or no. No leaves personal eligibility unknown and does not schedule private evaluation. Yes does not guess missing thresholds, citizenship, or cash, and unknown thresholds cannot become ready.", "answer_type": "bool", "required": true, "visibility_default": "private"}, {"field_key": "accept_conditional_ra_ta", "text": "Should conditional research or teaching appointments be considered? Answer yes, no, or null when that preference is still unknown. Null stays unknown.", "answer_type": "optional_bool", "required": true, "visibility_default": "private"}, {"field_key": "accept_competitive_awards", "text": "Should competitive awards be considered? Answer yes, no, or null when that preference is still unknown. Null stays unknown.", "answer_type": "optional_bool", "required": true, "visibility_default": "private"}, {"field_key": "max_net_total_cost", "text": "What is the maximum acceptable net total cost? Answer an amount and ISO currency, for example 1200 INR, or {\"amount\",\"currency\"}. Leave the question unanswered when the limit is unknown; unknown is not stored as zero.", "answer_type": "money", "required": true, "visibility_default": "private"}, {"field_key": "max_upfront_cash", "text": "What is the maximum upfront cash you can provide? Answer an amount and ISO currency. Leave the question unanswered when the limit is unknown; unknown is not stored as zero.", "answer_type": "money", "required": true, "visibility_default": "private"}, {"field_key": "min_living_adequacy_rule", "text": "What living-support rule should private evaluation use? Answer JSON with basis same_location_period, explicit booleans count_housing_once and count_insurance_once, and numeric_floor set to null or a money object. Null means there is no numeric floor.", "answer_type": "living_rule", "required": true, "visibility_default": "private"}, {"field_key": "max_work_hours_acceptable", "text": "What is the maximum acceptable work requirement per week, in hours? Answer a whole number from 0 through 168.", "answer_type": "hours", "required": true, "visibility_default": "private"}, {"field_key": "allow_loans", "text": "May loan awards be considered at all? Answer yes or no. A loan is not grant funding.", "answer_type": "bool", "required": true, "visibility_default": "private"}, {"field_key": "citizenship", "text": "What citizenship should private eligibility use? Answer one short plain label. No nationality is assumed.", "answer_type": "citizenship", "required": true, "visibility_default": "private"}, {"field_key": "prior_degree", "text": "What prior degree should private eligibility use? Answer JSON with level (bachelors, masters, doctoral, or other) and field.", "answer_type": "prior_degree", "required": true, "visibility_default": "private"}, {"field_key": "academic_record", "text": "Provide an academic record only when you want one stored. Answer JSON with grade and scale as decimal strings. The scale is kept as given and is not converted.", "answer_type": "academic_record", "required": false, "visibility_default": "private"}, {"field_key": "tests", "text": "Provide test records only when you want them stored. Answer a JSON list of objects with test, date as YYYY-MM-DD, and score {value, scale}. No score is guessed.", "answer_type": "tests", "required": false, "visibility_default": "private"}];
const CHOICES = {"degrees": ["masters", "phd", "other"], "modalities": ["on_campus", "online", "hybrid"], "prior": ["bachelors", "masters", "doctoral", "other"], "components": ["tuition", "mandatory_fees", "living_amount", "living_duration", "health_insurance", "visa_costs", "application_costs", "travel_costs", "deposits", "proof_of_funds_liquidity"], "work": ["recheck_official_sources", "refresh_cycle_dates", "refresh_public_funding_facts"], "counters": ["queued_nodes", "visited_nodes", "fetches", "queries", "model_tokens"], "structural": ["max_depth", "max_queue", "per_domain_inflight", "max_hypotheses_per_wave"], "structural_caps": {"max_depth": 64, "max_queue": 1000000, "per_domain_inflight": 10000, "max_hypotheses_per_wave": 10000}, "lifetime_cap": 1000000000};
const TAB_IDS = ['goal', 'shortlist', 'activity', 'evidence', 'graph'];
const EDITABLE = new Set(['draft', 'needs_information', 'contract_proposed', 'revising']);
const FRAME_KEYS = ['domain', 'public_objective', 'public_criteria', 'field_tags', 'regions', 'intake_windows', 'required_source_fields', 'frame_version'];
const STATUS_LABEL = { met: 'Met', unmet: 'Unmet', unknown: 'Unknown', conditional: 'Conditional', contested: 'Contested' };
const STATE_LABEL = {
  draft: 'Draft', needs_information: 'Needs information', contract_proposed: 'Contract proposed',
  approved: 'Approved', running: 'Running', pausing: 'Pausing', paused: 'Paused', revising: 'Revising',
  research_ready: 'Research ready', monitoring: 'Monitoring', stopped: 'Stopped',
};
const PROVENANCE = {
  user_confirmed: 'Confirmed by you', user_stated: 'Draft, not confirmed',
  imported_history: 'Imported history, not confirmed', system_default: 'System default, not confirmed',
};
const ASSURANCE = {
  reviewed_kept: 'Reviewed, kept', reviewed_dropped: 'Reviewed, dropped',
  code_checked: 'Quote checked; review still required', review_pending: 'Review pending',
  review_unavailable: 'Review unavailable', pending: 'Review pending', unavailable: 'Review unavailable',
};
const COMPONENT_LABEL = {
  tuition: 'Tuition', mandatory_fees: 'Mandatory fees', living_amount: 'Living amount',
  living_duration: 'Living duration', health_insurance: 'Health insurance', visa_costs: 'Visa costs',
  application_costs: 'Application costs', travel_costs: 'Travel costs', deposits: 'Deposits',
  proof_of_funds_liquidity: 'Proof-of-funds liquidity',
};
const WORK_LABEL = {
  recheck_official_sources: 'Recheck official sources', refresh_cycle_dates: 'Refresh cycle dates',
  refresh_public_funding_facts: 'Refresh public funding facts',
};
const replays = new Map();
let dialogState = null;
let trapHandler = null;

class ApiError extends Error {
  constructor(status, body) {
    const message = body && typeof body.message === 'string' ? body.message : (status ? 'Server answered HTTP ' + status : 'The request failed.');
    super(message);
    this.name = 'ApiError';
    this.status = status || 0;
    this.code = body && typeof body.error === 'string' ? body.error : '';
    this.details = body && body.details && typeof body.details === 'object' ? body.details : null;
  }
}

function uuid() {
  if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('').replace(/^(.{8})(.{4})(.{4})(.{4})(.{12})$/, '$1-$2-$3-$4-$5');
}
function isHash(value) { return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value); }
function isStale(err) {
  if (!err || !err.status) return false;
  if (err.status === 409) return true;
  return err.status === 422 && (err.code === 'hash_mismatch' || err.code === 'version_conflict');
}
function errorText(err) {
  if (!err) return 'The request failed.';
  let msg = err.message || 'The request failed.';
  const details = err.details;
  if (details && typeof details === 'object') {
    if (typeof details.field === 'string' && details.field && !msg.includes(details.field)) msg = details.field + ': ' + msg;
    if (typeof details.row_version === 'number') msg += ' Current version: ' + details.row_version + '.';
    if (typeof details.retry_after === 'number') msg += ' No automatic retry. Wait ' + details.retry_after + ' seconds.';
  }
  return msg;
}
function safeMessage(err, secret) {
  let msg = errorText(err);
  if (secret && msg.includes(secret)) msg = 'Sign-in failed.';
  return msg;
}
function csrf() { return state.csrf || ''; }
function showSession() {
  const node = $('refreshed');
  if (!node) return;
  if (!state.csrf) { node.textContent = 'not signed in'; return; }
  node.textContent = typeof state.sessionExpires === 'number'
    ? 'signed in until ' + fmtTime(state.sessionExpires)
    : 'signed in';
}
function rememberSession(csrf, expiresAt) {
  if (typeof csrf !== 'string' || !csrf) return false;
  if (typeof expiresAt !== 'number' || !Number.isFinite(expiresAt) || expiresAt <= 0) return false;
  state.csrf = csrf;
  state.sessionExpires = expiresAt;
  showSession();
  return true;
}
function takeSession(body) {
  // Signed-in GET is exactly {authenticated:true, expires_at, csrf}. A 401 is not a body.
  if (!body || body.authenticated !== true) return false;
  return rememberSession(body.csrf, body.expires_at);
}
function takeLogin(body) {
  // POST returns only {csrf, expires_at}. authenticated:false is not a session.
  if (!body || body.authenticated === false) return false;
  return rememberSession(body.csrf, body.expires_at);
}
function goalRecord() { return state.goal && state.goal.goal ? state.goal.goal : null; }
function goalId() { const goal = goalRecord(); return goal && typeof goal.id === 'number' ? goal.id : null; }
function rowVersion() {
  if (state.interview && Number.isInteger(state.interview.row_version)) return state.interview.row_version;
  const goal = goalRecord();
  return goal && Number.isInteger(goal.row_version) ? goal.row_version : null;
}
function questionByKey(key) {
  const server = ((state.interview && state.interview.questions) || []).find((item) => item && item.field_key === key);
  const local = BANK.find((item) => item.field_key === key);
  if (server && local) return Object.assign({}, local, server);
  return server || local || null;
}
function fieldByKey(key) {
  const fields = (state.interview && state.interview.fields) || [];
  return fields.find((item) => item && item.key === key) || null;
}
function formatValue(value) {
  if (value === null) return 'null';
  if (value === undefined) return 'not recorded';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try { return JSON.stringify(value, null, 2); } catch (_) { return 'Unreadable value'; }
}
function when(sec) { return typeof sec === 'number' ? fmtTime(sec) : 'not recorded'; }
function labelState(name) { return STATE_LABEL[name] || name || 'State not recorded'; }
function provenanceLabel(name) { return PROVENANCE[name] || name || 'provenance not recorded'; }
function assuranceLabel(name) { return !name ? 'Assurance not recorded' : (ASSURANCE[name] || name); }

async function api(path, opts) {
  const options = opts || {};
  const method = options.method || 'GET';
  const headers = { Accept: 'application/json' };
  let payload;
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(options.body);
  }
  const loginPost = method === 'POST' && path === '/api/session';
  if (!loginPost && method !== 'GET' && method !== 'HEAD') headers['X-Snowball-CSRF'] = csrf();
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 30000);
  try {
    const res = await fetch(path, {
      method, headers, body: payload, credentials: 'same-origin', cache: 'no-store', signal: ctl.signal,
    });
    if (res.status === 204) return null;
    const text = await res.text();
    let body = null;
    if (text) { try { body = JSON.parse(text); } catch (_) { body = null; } }
    if (!res.ok) {
      const err = new ApiError(res.status, body || { error: 'error', message: 'Server answered HTTP ' + res.status });
      if (res.status === 401 && path !== '/api/session') { dropSession(); openDialog('login-dialog', $('login-token')); }
      throw err;
    }
    if (body === null) throw new ApiError(res.status, { error: 'error', message: 'The server returned a response that is not JSON.' });
    return body;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err && err.name === 'AbortError') throw new ApiError(0, { error: 'timeout', message: 'The request timed out after 30 seconds.' });
    throw new ApiError(0, { error: 'unavailable', message: 'Could not reach the console. The local server may be stopped.' });
  } finally { clearTimeout(timer); }
}
function assertFresh() {
  if (!state.stale) return;
  throw new ApiError(409, { error: 'version_conflict', message: 'Reload the current record before trying again. The last action was not retried.' });
}
async function postOnce(path, body) {
  assertFresh();
  return api(path, { method: 'POST', body });
}
async function postIdempotent(path, body) {
  assertFresh();
  const serial = JSON.stringify(body);
  let slot = replays.get(path);
  if (!slot || slot.settled) {
    slot = { body: serial, key: uuid(), settled: false, inflight: false };
    replays.set(path, slot);
  } else if (slot.body !== serial) {
    throw new ApiError(0, { error: 'replay', message: 'A previous request on this action did not finish. Reload and review the current record before sending a different one. It was not retried.' });
  }
  if (slot.inflight) throw new ApiError(0, { error: 'busy', message: 'That action is already being sent.' });
  slot.inflight = true;
  try {
    const result = await api(path, { method: 'POST', body, idempotencyKey: slot.key });
    slot.settled = true;
    return result;
  } catch (err) {
    if (isStale(err) || err.status) slot.settled = true;
    throw err;
  } finally { slot.inflight = false; }
}
function handleWriteError(err, node) {
  if (isStale(err)) { showStale(err); return true; }
  if (node) node.textContent = errorText(err);
  else showTop(errorText(err));
  return false;
}

function focusable(root) {
  return [...root.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])')]
    .filter((node) => !node.closest('[hidden]'));
}
function openDialog(id, initial) {
  const dialog = $(id);
  if (!dialog) return;
  const back = dialogState ? dialogState.back : document.activeElement;
  if (dialogState && dialogState.dialog !== dialog) dialogState.dialog.hidden = true;
  dialog.hidden = false;
  dialogState = { dialog, back };
  const start = initial && dialog.contains(initial) ? initial : (dialog.querySelector('h2') || focusable(dialog)[0] || dialog);
  start.focus();
  if (trapHandler) document.removeEventListener('keydown', trapHandler, true);
  trapHandler = (ev) => {
    if (!dialogState || dialogState.dialog !== dialog || dialog.hidden) return;
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeDialog(id); return; }
    if (ev.key !== 'Tab') return;
    const list = focusable(dialog);
    if (!list.length) { ev.preventDefault(); return; }
    const first = list[0];
    const last = list[list.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  };
  document.addEventListener('keydown', trapHandler, true);
}
function closeDialog(id) {
  const dialog = $(id);
  if (dialog) dialog.hidden = true;
  if (trapHandler) { document.removeEventListener('keydown', trapHandler, true); trapHandler = null; }
  const back = dialogState && dialogState.back;
  dialogState = null;
  if (back && typeof back.focus === 'function' && document.contains(back)) back.focus();
}
function showStale(err) {
  state.stale = true;
  const approve = $('review-approve');
  if (approve) approve.disabled = true;
  $('stale-text').textContent = errorText(err) + ' This action was not retried.';
  openDialog('stale-dialog', $('stale-reload'));
  announce('The record changed. Reload before trying again.');
}
function showTop(message) {
  const host = $('top-banner');
  if (!message) { host.replaceChildren(); return; }
  host.replaceChildren(banner('bad', 'The console could not finish that request', message));
}
function banner(kind, title, detail) {
  return el('div', { class: 'banner ' + kind, role: kind === 'bad' ? 'alert' : 'status' },
    el('h3', null, title), detail ? el('p', null, detail) : null);
}
function clearPrivate() {
  state.goal = null; state.interview = null; state.events = null; state.shortlist = null;
  state.activity = null; state.evidence = null; state.review = null; state.forcedKey = '';
  state.evidenceKey = ''; state.evidenceField = '';
  for (const id of ['goal-banners', 'goal-create', 'interview-body', 'contract-body', 'grant-body', 'control-body', 'events-body', 'short-banners', 'short-body', 'activity-banners', 'activity-body', 'evidence-banners', 'evidence-body', 'review-body', 'graph-banners']) {
    const node = $(id); if (node) node.replaceChildren();
  }
  const badge = $('goal-badge'); if (badge) badge.textContent = '';
  const hash = $('review-hash'); if (hash) hash.textContent = '';
  const stop = $('stop-reason'); if (stop) stop.value = '';
}
function dropSession() {
  state.csrf = '';
  state.sessionExpires = null;
  state.stale = false;
  replays.clear();
  for (const id of ['review-dialog', 'stale-dialog', 'stop-dialog']) {
    const dialog = $(id);
    if (dialog) dialog.hidden = true;
  }
  if (trapHandler) { document.removeEventListener('keydown', trapHandler, true); trapHandler = null; }
  dialogState = null;
  clearPrivate();
  const out = $('signout'); if (out) out.hidden = true;
  const inn = $('signin'); if (inn) inn.hidden = false;
  showSession();
}
function signedOutCopy() {
  return stateBox('empty', 'Sign in required', 'Goal, shortlist, activity, and evidence stay hidden until the operator session is accepted.');
}

function readInt(input, low, high) {
  if (!input) return null;
  const text = input.value.trim();
  if (!/^(?:0|[1-9][0-9]*)$/.test(text)) return null;
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < low || value > high) return null;
  return value;
}
function readAmount(input) {
  const text = input.value.trim();
  return /^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$/.test(text) ? text : null;
}
function readCurrency(input) {
  const text = input.value.trim();
  return /^[A-Z]{3}$/.test(text) ? text : null;
}
function readShare(input) {
  if (!input) return null;
  const text = input.value.trim();
  if (!/^(?:0|1|0\.[0-9]{1,6}|1\.0{1,6})$/.test(text)) return null;
  const dot = text.indexOf('.');
  if (dot < 0) return Number(text);
  const frac = text.slice(dot + 1).replace(/0+$/, '');
  if (!frac) return Number(text.slice(0, dot));
  return text.slice(0, dot) + '.' + frac;
}
function shareAmount(value) {
  if (typeof value === 'number') return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(text)) return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1 ? parsed : null;
}
function shareLabel(value) {
  if (shareAmount(value) === null) return '';
  return typeof value === 'string' ? value.trim() : String(value);
}
function readLabel(text) {
  const value = text.trim();
  if (!value || value.length > 80 || [...value].some((ch) => ch.charCodeAt(0) < 32)) return null;
  return value;
}
function radioSet(name, options, selected) {
  const inputs = [];
  const box = el('div', { class: 'choices', role: 'radiogroup' });
  for (const option of options) {
    const input = el('input', { type: 'radio', name, value: option.value });
    if (selected !== undefined && selected !== null && String(selected) === option.value) input.checked = true;
    inputs.push(input);
    box.append(el('label', { class: 'chk' }, input, option.label));
  }
  return { box, inputs, read() { const picked = inputs.find((input) => input.checked); return picked ? picked.value : null; } };
}
function textInput(label, value, attrs) {
  const input = el('input', Object.assign({ type: 'text' }, attrs || {}));
  if (value !== undefined && value !== null) input.value = String(value);
  return { input, node: el('label', { class: 'field' }, label, input) };
}
function lineList(label, values) {
  const area = el('textarea', { rows: '5' });
  if (Array.isArray(values)) area.value = values.join('\n');
  return { area, node: el('label', { class: 'field' }, label, area) };
}
function moneyInputs(prefix, current) {
  const amount = textInput(prefix + ' amount', current && current.amount, { maxlength: '16', autocomplete: 'off' });
  const currency = textInput(prefix + ' currency', current && current.currency, { maxlength: '3', autocomplete: 'off' });
  return { amount, currency, node: el('div', { class: 'inline' }, amount.node, currency.node) };
}
function intakeRows(values) {
  const host = el('div', { class: 'stack' });
  function add(item) {
    const year = el('input', { type: 'text', inputmode: 'numeric', maxlength: '4', 'aria-label': 'Intake year' });
    const season = el('select', { 'aria-label': 'Intake season' });
    season.append(el('option', { value: '' }, 'Season'), ...['fall', 'spring', 'summer', 'winter'].map((name) => el('option', { value: name }, name)));
    if (typeof item === 'string' && /^[0-9]{4}-/.test(item)) {
      year.value = item.slice(0, 4);
      season.value = item.slice(5);
    }
    const remove = el('button', { class: 'btn', type: 'button' }, 'Remove');
    const row = el('div', { class: 'inline' }, year, season, remove);
    remove.addEventListener('click', () => row.remove());
    host.append(row);
  }
  const seed = Array.isArray(values) && values.length ? values : [''];
  seed.forEach(add);
  const more = el('button', { class: 'btn', type: 'button' }, 'Add intake');
  more.addEventListener('click', () => add(''));
  return { host, more, read() {
    const values = [];
    for (const row of host.children) {
      const year = row.querySelector('input').value.trim();
      const season = row.querySelector('select').value;
      if (!year && !season) continue;
      if (!/^(?:19|20|21)[0-9]{2}$/.test(year) || !season) return { ok: false, message: 'Each intake needs a year and a season, such as 2028-fall.' };
      values.push(year + '-' + season);
    }
    if (!values.length) return { ok: false, message: 'Enter at least one intake window.' };
    return { ok: true, value: values };
  } };
}
function testRows(values) {
  const host = el('div', { class: 'stack' });
  function add(item) {
    const name = textInput('Test', item && item.test, { maxlength: '80' });
    const date = textInput('Date', item && item.date, { maxlength: '10', placeholder: 'YYYY-MM-DD' });
    const score = textInput('Score', item && item.score && item.score.value);
    const scale = textInput('Scale', item && item.score && item.score.scale);
    const remove = el('button', { class: 'btn', type: 'button' }, 'Remove');
    const row = el('div', { class: 'stack' }, el('div', { class: 'inline' }, name.node, date.node, score.node, scale.node, remove));
    row._parts = { name, date, score, scale };
    remove.addEventListener('click', () => row.remove());
    host.append(row);
  }
  (Array.isArray(values) && values.length ? values : [{}]).forEach(add);
  const more = el('button', { class: 'btn', type: 'button' }, 'Add test');
  more.addEventListener('click', () => add(null));
  return { host, more, read() {
    const rows = [...host.children].filter((row) => row._parts);
    if (!rows.length) return { ok: false, message: 'Add a test record or skip this optional question.' };
    const value = [];
    for (const row of rows) {
      const name = readLabel(row._parts.name.input.value);
      const date = row._parts.date.input.value.trim();
      const score = readAmount(row._parts.score.input);
      const scale = readAmount(row._parts.scale.input);
      if (!name || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(date) || score === null || scale === null) {
        return { ok: false, message: 'Each test needs a name, a YYYY-MM-DD date, and a score value and scale.' };
      }
      value.push({ test: name, date, score: { value: score, scale } });
    }
    return { ok: true, value };
  } };
}
function componentForm(current) {
  const rows = [];
  const box = el('div', { class: 'stack' });
  for (const key of CHOICES.components) {
    const selected = current && typeof current[key] === 'boolean' ? String(current[key]) : null;
    const radios = radioSet('component-' + key, [{ value: 'true', label: 'Yes' }, { value: 'false', label: 'No' }], selected);
    rows.push({ key, radios });
    box.append(el('fieldset', { class: 'stack' }, el('legend', null, COMPONENT_LABEL[key] || key), radios.box));
  }
  return { node: box, read() {
    const value = {};
    for (const row of rows) {
      const picked = row.radios.read();
      if (picked !== 'true' && picked !== 'false') return { ok: false, message: 'Choose yes or no for every funding component. Nothing is preselected.' };
      value[row.key] = picked === 'true';
    }
    return { ok: true, value };
  } };
}
function boolPick(value) { return value === true ? 'true' : value === false ? 'false' : null; }
function livingForm(current) {
  const housing = radioSet('housing-once', [{ value: 'true', label: 'Yes' }, { value: 'false', label: 'No' }], current ? boolPick(current.count_housing_once) : null);
  const insurance = radioSet('insurance-once', [{ value: 'true', label: 'Yes' }, { value: 'false', label: 'No' }], current ? boolPick(current.count_insurance_once) : null);
  const floorOn = el('input', { type: 'checkbox' });
  const money = moneyInputs('Numeric floor', current && current.numeric_floor);
  money.node.hidden = !(current && current.numeric_floor);
  if (current && current.numeric_floor) floorOn.checked = true;
  floorOn.addEventListener('change', () => { money.node.hidden = !floorOn.checked; });
  return { node: el('div', { class: 'stack' },
    el('p', { class: 'note' }, 'Basis is same location and period. Housing and insurance each need an explicit counting choice.'),
    el('fieldset', null, el('legend', null, 'Count housing once'), housing.box),
    el('fieldset', null, el('legend', null, 'Count insurance once'), insurance.box),
    el('label', { class: 'chk' }, floorOn, 'Set a numeric floor'),
    money.node), read() {
    const housingValue = housing.read();
    const insuranceValue = insurance.read();
    if (housingValue === null || insuranceValue === null) return { ok: false, message: 'Choose yes or no for both counting flags.' };
    let numeric = null;
    if (floorOn.checked) {
      const amount = readAmount(money.amount.input);
      const currency = readCurrency(money.currency.input);
      if (amount === null || currency === null) return { ok: false, message: 'A numeric floor needs a nonnegative amount and an uppercase currency code.' };
      numeric = { amount, currency };
    }
    return { ok: true, value: { basis: 'same_location_period', count_housing_once: housingValue === 'true', count_insurance_once: insuranceValue === 'true', numeric_floor: numeric } };
  } };
}
function policyForm(current) {
  const inputs = {};
  const grid = el('div', { class: 'stack' });
  const limits = (current && current.lifetime_limits) || {};
  for (const counter of CHOICES.counters) {
    const field = textInput(counter.replaceAll('_', ' '), limits[counter], { inputmode: 'numeric' });
    inputs[counter] = field.input;
    grid.append(field.node);
  }
  for (const name of CHOICES.structural) {
    const cap = CHOICES.structural_caps[name];
    const field = textInput(name.replaceAll('_', ' ') + (cap ? ' (ceiling ' + cap + ')' : ''), current && current[name], { inputmode: 'numeric' });
    inputs[name] = field.input;
    grid.append(field.node);
  }
  const share = textInput('Exploration share from 0 to 1', current && current.exploration_share);
  inputs.exploration_share = share.input;
  const mode = el('select');
  mode.append(el('option', { value: '' }, 'Usage mode'), el('option', { value: 'estimated' }, 'Estimated'), el('option', { value: 'hard' }, 'Hard, unattested'));
  if (current && (current.usage_mode === 'estimated' || current.usage_mode === 'hard')) mode.value = current.usage_mode;
  return { node: el('div', { class: 'stack' }, grid, share.node, el('label', { class: 'field' }, 'Usage mode', mode),
    el('p', { class: 'note' }, 'Hard records the choice only. It does not create an attested provider-token cap, and a grant cannot use hard mode.')), read() {
    const lifetime = {};
    for (const counter of CHOICES.counters) {
      const value = readInt(inputs[counter], 0, CHOICES.lifetime_cap);
      if (value === null) return { ok: false, message: 'Every lifetime ceiling must be a whole number from 0 through 1000000000.' };
      lifetime[counter] = value;
    }
    const structural = {};
    for (const name of CHOICES.structural) {
      const cap = CHOICES.structural_caps[name];
      const value = readInt(inputs[name], 0, cap);
      if (value === null) return { ok: false, message: name.replaceAll('_', ' ') + ' must be a whole number from 0 through ' + cap + '.' };
      structural[name] = value;
    }
    const exploration = readShare(inputs.exploration_share);
    if (exploration === null) return { ok: false, message: 'Exploration share must be a number from 0 to 1.' };
    if (mode.value !== 'estimated' && mode.value !== 'hard') return { ok: false, message: 'Choose estimated or hard usage mode.' };
    return { ok: true, value: Object.assign({ lifetime_limits: lifetime, exploration_share: exploration, usage_mode: mode.value }, structural) };
  } };
}
function monitoringForm(current) {
  const selected = new Set((current && current.permitted_work) || []);
  const boxes = CHOICES.work.map((token) => {
    const input = el('input', { type: 'checkbox', value: token });
    input.checked = selected.has(token);
    return { token, input, node: el('label', { class: 'chk' }, input, WORK_LABEL[token] || token) };
  });
  const refreshes = textInput('Maximum refreshes', current && current.max_refreshes, { inputmode: 'numeric' });
  const interval = textInput('Interval in days', current && current.interval_days, { inputmode: 'numeric' });
  return { node: el('div', { class: 'stack' }, el('fieldset', null, el('legend', null, 'Permitted refresh work'), ...boxes.map((box) => box.node)), refreshes.node, interval.node,
    el('p', { class: 'note' }, 'This describes permitted refresh work. It does not start monitoring.')), read() {
    const work = boxes.filter((box) => box.input.checked).map((box) => box.token);
    if (!work.length) return { ok: false, message: 'Choose at least one permitted refresh action.' };
    const maxRefreshes = readInt(refreshes.input, 1, 100000);
    const days = readInt(interval.input, 1, 3660);
    if (maxRefreshes === null || days === null) return { ok: false, message: 'Maximum refreshes and interval days must be whole numbers inside the supported bounds.' };
    return { ok: true, value: { permitted_work: work, max_refreshes: maxRefreshes, interval_days: days } };
  } };
}
function priorForm(current) {
  const level = el('select');
  level.append(el('option', { value: '' }, 'Level'), ...CHOICES.prior.map((item) => el('option', { value: item }, item)));
  if (current && CHOICES.prior.includes(current.level)) level.value = current.level;
  const field = textInput('Field', current && current.field, { maxlength: '80' });
  return { node: el('div', { class: 'stack' }, el('label', { class: 'field' }, 'Prior degree level', level), field.node), read() {
    if (!level.value) return { ok: false, message: 'Choose a prior degree level.' };
    const name = readLabel(field.input.value);
    if (!name) return { ok: false, message: 'Enter the prior degree field as a short label.' };
    return { ok: true, value: { level: level.value, field: name } };
  } };
}
function academicForm(current) {
  const grade = textInput('Grade', current && current.grade);
  const scale = textInput('Scale', current && current.scale);
  return { node: el('div', { class: 'inline' }, grade.node, scale.node), read() {
    const gradeValue = readAmount(grade.input);
    const scaleValue = readAmount(scale.input);
    if (gradeValue === null || scaleValue === null) return { ok: false, message: 'Enter the original grade and scale. No conversion is applied.' };
    return { ok: true, value: { grade: gradeValue, scale: scaleValue } };
  } };
}
function buildAnswer(question, current) {
  const key = question.field_key;
  const value = current ? current.value : undefined;
  const wrap = el('div', { class: 'answer stack' });
  let read = () => ({ ok: false, message: 'This question has no answer yet.' });
  if (question.answer_type === 'enum') {
    const choices = Array.isArray(question.choices) ? question.choices : [];
    const select = el('select');
    select.append(el('option', { value: '' }, 'Choose'), ...choices.map((item) => el('option', { value: String(item) }, String(item))));
    if (typeof value === 'string' && choices.includes(value)) select.value = value;
    wrap.append(el('label', { class: 'field' }, 'Answer', select));
    read = () => select.value ? { ok: true, value: select.value } : { ok: false, message: choices.length ? 'Choose one of the listed answers.' : 'This question did not include its choices.' };
  } else if (key === 'field_tags' || key === 'regions') {
    const lines = lineList(key === 'regions' ? 'Regions, one per line. An empty list is not worldwide.' : 'Field tags, one per line', value);
    wrap.append(lines.node);
    read = () => {
      const items = lines.area.value.split(/\n/).map((item) => item.trim()).filter(Boolean);
      if (key === 'field_tags' && !items.length) return { ok: false, message: 'Enter at least one field tag.' };
      if (items.some((item) => readLabel(item) === null)) return { ok: false, message: 'Each entry must be a short label without control characters.' };
      return { ok: true, value: items };
    };
  } else if (key === 'intake_windows') {
    const rows = intakeRows(value);
    wrap.append(rows.host, rows.more);
    read = () => rows.read();
  } else if (key === 'tests') {
    const rows = testRows(value);
    wrap.append(rows.host, rows.more);
    read = () => rows.read();
  } else if (question.answer_type === 'bool' || question.answer_type === 'consent') {
    const options = [{ value: 'true', label: 'Yes' }, { value: 'false', label: 'No' }];
    if (question.allow_unknown && question.answer_type === 'bool') options.push({ value: 'null', label: 'Unknown' });
    const selected = value === true ? 'true' : value === false ? 'false' : value === null && question.allow_unknown ? 'null' : null;
    const radios = radioSet('answer-' + key, options, selected);
    wrap.append(radios.box);
    if (key === 'scope_broad_acknowledged') wrap.append(el('p', { class: 'note' }, 'No keeps empty regions unconfirmed. Empty regions are not worldwide.'));
    if (question.answer_type === 'consent') wrap.append(el('p', { class: 'note' }, 'No leaves private eligibility evaluation unscheduled. Public program research stays separate.'));
    read = () => {
      const picked = radios.read();
      if (picked === null) return { ok: false, message: 'Choose yes or no. Nothing is selected for you.' };
      if (picked === 'null') return { ok: true, value: null };
      return { ok: true, value: picked === 'true' };
    };
  } else if (question.answer_type === 'money') {
    const money = moneyInputs('Amount', value && typeof value === 'object' ? value : null);
    wrap.append(money.node, el('p', { class: 'note' }, 'Leave this unanswered when the limit is unknown. Unknown is not stored as zero.'));
    read = () => {
      const amount = readAmount(money.amount.input);
      const currency = readCurrency(money.currency.input);
      if (amount === null || currency === null) return { ok: false, message: 'Enter a nonnegative amount and a three-letter uppercase currency code.' };
      return { ok: true, value: { amount, currency } };
    };
  } else if (key === 'max_work_hours_acceptable' || key === 'completion_min_candidates') {
    const bounds = key === 'max_work_hours_acceptable' ? [0, 168] : [1, 100000];
    const field = textInput(key === 'max_work_hours_acceptable' ? 'Hours per week' : 'Matching opportunities required', typeof value === 'number' ? value : '', { inputmode: 'numeric' });
    wrap.append(field.node);
    read = () => {
      const number = readInt(field.input, bounds[0], bounds[1]);
      if (number === null) return { ok: false, message: 'Enter a whole number from ' + bounds[0] + ' through ' + bounds[1] + '.' };
      return { ok: true, value: number };
    };
  } else if (key === 'citizenship') {
    const field = textInput('Citizenship', typeof value === 'string' ? value : '', { maxlength: '80' });
    wrap.append(field.node);
    read = () => { const label = readLabel(field.input.value); return label ? { ok: true, value: label } : { ok: false, message: 'Enter one short citizenship label. None is assumed.' }; };
  } else if (key === 'required_components') {
    const form = componentForm(value && typeof value === 'object' ? value : null);
    wrap.append(form.node); read = () => form.read();
  } else if (key === 'min_living_adequacy_rule') {
    const form = livingForm(value && typeof value === 'object' ? value : null);
    wrap.append(form.node); read = () => form.read();
  } else if (key === 'prior_degree') {
    const form = priorForm(value && typeof value === 'object' ? value : null);
    wrap.append(form.node); read = () => form.read();
  } else if (key === 'academic_record') {
    const form = academicForm(value && typeof value === 'object' ? value : null);
    wrap.append(form.node, el('p', { class: 'note' }, 'Optional. The scale is stored as given and is not converted.'));
    read = () => form.read();
  } else if (key === 'execution_policy') {
    const form = policyForm(value && typeof value === 'object' ? value : null);
    wrap.append(form.node); read = () => form.read();
  } else if (key === 'monitoring') {
    const form = monitoringForm(value && typeof value === 'object' ? value : null);
    wrap.append(form.node); read = () => form.read();
  } else {
    const area = el('textarea', { rows: '6' });
    if (value !== undefined) area.value = formatValue(value);
    wrap.append(el('label', { class: 'field' }, 'JSON value', area), el('p', { class: 'note' }, 'The server checks this value. Nothing is guessed.'));
    read = () => { try { return { ok: true, value: JSON.parse(area.value) }; } catch (_) { return { ok: false, message: 'Enter JSON. The server checks it; nothing is guessed.' }; } };
  }
  const visibility = question.visibility_default === 'public_ok' ? 'public_ok' : 'private';
  wrap.append(el('p', { class: 'note' }, visibility === 'private'
    ? 'Visibility is private. This answer stays in the interview and contract review and cannot enter the public research frame.'
    : 'Visibility is public. This answer is part of the public research frame and cannot be hidden.'));
  wrap._read = read;
  wrap._visibility = visibility;
  return wrap;
}

function activeQuestion() {
  if (state.forcedKey) return questionByKey(state.forcedKey);
  return state.interview && state.interview.next;
}
function renderInterview() {
  const host = $('interview-body');
  const goal = goalRecord();
  const data = state.interview;
  if (!goal) { host.replaceChildren(); return; }
  if (!data) { host.replaceChildren(stateBox('empty', 'Interview is not loaded', 'Refresh to read the next question.')); return; }
  const editable = EDITABLE.has(goal.state);
  const parts = [el('h3', null, 'Interview')];
  if (goal.state === 'contract_proposed') parts.push(el('p', { class: 'note' }, 'Saving an answer discards the current proposal. The approved contract, if any, stays frozen.'));
  if (goal.state === 'revising') parts.push(el('p', { class: 'note' }, 'The approved contract stays in force until a new version is approved. Canceling the revision does not resume running work.'));
  if (!editable) parts.push(el('p', { class: 'note' }, 'Answers are locked while this goal state is ' + labelState(goal.state) + '. Revise the contract before changing them.'));
  const missing = Array.isArray(data.missing) ? data.missing : null;
  if (missing && missing.length) parts.push(banner('wait', 'Needs information', 'Still unconfirmed: ' + missing.join(', ') + '.'));
  else if (missing) parts.push(el('p', { class: 'note' }, 'No required answer is waiting.'));
  const question = editable ? activeQuestion() : null;
  if (question) parts.push(questionForm(question));
  else if (editable && !data.next) parts.push(stateBox('empty', 'No question is waiting', 'Required answers are confirmed, or no further question applies. Propose a contract to freeze them.'));
  parts.push(answerList(data, editable));
  parts.push(optionalAdds(data, editable));
  parts.push(turnList(data.turns));
  host.replaceChildren(...parts);
}
function questionForm(question) {
  const current = fieldByKey(question.field_key);
  const answer = buildAnswer(question, current);
  const confirm = el('input', { type: 'checkbox' });
  const error = el('p', { class: 'form-error', role: 'alert' });
  const save = el('button', { class: 'btn', type: 'submit' }, 'Save draft');
  const lock = el('button', { class: 'btn', type: 'button' }, 'Confirm answer');
  const unknown = question.allow_unknown && question.answer_type === 'bool'
    ? el('button', { class: 'btn', type: 'button' }, 'Confirm unknown') : null;
  const back = state.forcedKey ? el('button', { class: 'btn', type: 'button' }, 'Back to the waiting question') : null;
  const form = el('form', { id: 'interview-form', class: 'stack', tabindex: '-1' },
    el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, question.required ? 'Required' : 'Optional'), el('span', { class: 'tag' }, answer._visibility === 'private' ? 'Private' : 'Public frame')),
    el('h3', null, question.text || question.field_key),
    current ? el('p', null, 'Recorded value: ', el('span', { class: 'mono' }, formatValue(current.value))) : null,
    current ? el('p', { class: 'note' }, 'Status: ' + (current.status || 'not recorded') + '. ' + provenanceLabel(current.provenance) + '.') : null,
    answer, el('label', { class: 'chk' }, confirm, 'Confirm this answer as known'), error,
    el('div', { class: 'btn-row' }, save, lock, unknown, back));
  form.addEventListener('submit', (ev) => { ev.preventDefault(); submitAnswer('draft', answer, confirm, error); });
  lock.addEventListener('click', () => submitAnswer('confirm', answer, confirm, error));
  if (unknown) unknown.addEventListener('click', () => submitAnswer('unknown', answer, confirm, error));
  if (back) back.addEventListener('click', () => { state.forcedKey = ''; renderInterview(); });
  return form;
}
async function submitAnswer(mode, answer, confirmBox, error) {
  const question = activeQuestion();
  if (!question) return;
  error.textContent = '';
  let value;
  let confirm = false;
  if (mode === 'unknown') {
    if (!(question.allow_unknown && question.answer_type === 'bool')) { error.textContent = 'Unknown is not a valid stored answer for this question.'; return; }
    if (!confirmBox.checked) { error.textContent = 'Check the confirmation box to record unknown.'; return; }
    value = null; confirm = true;
  } else {
    const parsed = answer._read();
    if (!parsed.ok) { error.textContent = parsed.message; return; }
    value = parsed.value;
    confirm = mode === 'confirm';
    if (confirm && !confirmBox.checked) { error.textContent = 'Check the confirmation box before locking this answer.'; return; }
  }
  const version = rowVersion();
  const id = goalId();
  if (!Number.isInteger(version) || id === null) { error.textContent = 'The goal version is not loaded. Reload before answering.'; return; }
  try {
    await postOnce('/api/goals/' + id + '/interview/answers', {
      field_key: question.field_key, value, confirm, visibility: answer._visibility, expected_row_version: version,
    });
    state.forcedKey = '';
    announce(confirm ? 'Answer confirmed.' : 'Draft saved.');
    await refreshAll();
  } catch (err) { handleWriteError(err, error); }
}
function answerList(data, editable) {
  const fields = Array.isArray(data.fields) ? data.fields : null;
  if (!fields) return el('p', null, 'Recorded answers were not returned.');
  if (!fields.length) return el('p', null, 'No answers are recorded yet.');
  const byKey = new Map(fields.map((field) => [field.key, field]));
  const ordered = [];
  const seen = new Set();
  for (const question of (data.questions || [])) {
    if (byKey.has(question.field_key)) { ordered.push(byKey.get(question.field_key)); seen.add(question.field_key); }
  }
  for (const field of fields) if (!seen.has(field.key)) ordered.push(field);
  return el('div', { class: 'stack' }, el('h3', null, 'Recorded answers'), ...ordered.map((field) => {
    const change = editable ? el('button', { class: 'btn', type: 'button' }, 'Change') : null;
    if (change) change.addEventListener('click', () => { state.forcedKey = field.key; renderInterview(); const form = $('interview-form'); if (form) if (form) { form.tabIndex = -1; form.focus(); } });
    return el('article', { class: 'snap' },
      el('h4', { class: 'mono' }, field.key || 'key not recorded'),
      el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, field.visibility || 'visibility not recorded'), el('span', { class: 'tag' }, field.status || 'status not recorded'), el('span', { class: 'tag' }, provenanceLabel(field.provenance))),
      el('pre', { class: 'mono' }, formatValue(field.value)), change);
  }));
}
function optionalAdds(data, editable) {
  if (!editable) return null;
  const present = new Set((data.fields || []).map((field) => field.key));
  const buttons = ['academic_record', 'tests'].filter((key) => !present.has(key)).map((key) => {
    const button = el('button', { class: 'btn', type: 'button' }, key === 'tests' ? 'Add test scores' : 'Add academic record');
    button.addEventListener('click', () => { state.forcedKey = key; renderInterview(); });
    return button;
  });
  if (!buttons.length) return null;
  return el('div', { class: 'stack' }, el('h3', null, 'Optional records'), el('p', { class: 'note' }, 'Academic and test records are stored only when you add them. They are private.'), el('div', { class: 'btn-row' }, ...buttons));
}
function turnList(turns) {
  if (!Array.isArray(turns)) return el('p', null, 'The interview transcript was not returned.');
  if (!turns.length) return el('p', null, 'No interview turns are recorded yet.');
  const shown = turns.slice(-30);
  const note = turns.length > 30 ? el('p', { class: 'note' }, 'Showing the latest 30 of ' + turns.length + ' recorded turns.') : null;
  return el('div', { class: 'stack' }, el('h3', null, 'Transcript'), note, ...shown.map((turn) => el('article', { class: 'snap' },
    el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, turn.kind || 'turn'), el('span', { class: 'mono' }, turn.field_key || ''), el('span', null, when(turn.created_at))),
    el('pre', { class: 'mono' }, turn.text || ''))));
}
function contracts() {
  const list = state.goal && Array.isArray(state.goal.contracts) ? state.goal.contracts : null;
  if (!list) return null;
  return { list, approved: list.find((item) => item.status === 'approved') || null, proposed: list.find((item) => item.status === 'proposed') || null };
}
function renderContract() {
  const host = $('contract-body');
  const goal = goalRecord();
  if (!goal) { host.replaceChildren(); return; }
  const found = contracts();
  if (!found) { host.replaceChildren(stateBox('error', 'Contracts were not returned', 'Approval stays unavailable until the contract list loads.')); return; }
  const parts = [el('h3', null, 'Contract')];
  parts.push(found.approved ? contractCard(found.approved, 'Approved') : el('p', null, 'No approved contract is recorded.'));
  parts.push(found.proposed ? contractCard(found.proposed, 'Proposed') : el('p', null, 'No proposed contract is waiting.'));
  if (EDITABLE.has(goal.state)) {
    const waiting = state.interview && (state.interview.next || (state.interview.missing || []).length);
    const button = el('button', { class: 'btn', type: 'button' }, 'Propose contract from confirmed answers');
    button.disabled = !state.interview || !!waiting;
    button.addEventListener('click', proposeContract);
    parts.push(el('p', { class: 'note' }, waiting ? 'Confirm the waiting answers before proposing.' : 'Proposing freezes confirmed answers. It does not approve them or start research.'));
    parts.push(button);
  }
  if (found.proposed) {
    const review = el('button', { class: 'btn', type: 'button' }, 'Review changes and hash');
    review.addEventListener('click', () => openReview(found.proposed, found.approved));
    parts.push(review);
  }
  host.replaceChildren(...parts);
}
function contractCard(contract, label) {
  return el('article', { class: 'snap' },
    el('h4', null, label + ' version ' + (Number.isInteger(contract.version) ? contract.version : 'not recorded')),
    el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, contract.status || 'status not recorded')),
    el('p', { class: 'mono' }, isHash(contract.body_hash) ? contract.body_hash : 'Hash not recorded'),
    el('h4', null, 'Public research frame'), renderFrame(contract.frame));
}
function renderFrame(frame) {
  if (!frame || typeof frame !== 'object') return el('p', null, 'The public frame was not returned.');
  const rows = [];
  for (const key of FRAME_KEYS) {
    if (key in frame) rows.push(el('dt', null, key), el('dd', null, el('pre', { class: 'mono' }, formatValue(frame[key]))));
  }
  return rows.length ? el('dl', { class: 'facts' }, ...rows) : el('p', null, 'The public frame had no recognized fields.');
}
async function proposeContract() {
  const goal = goalRecord();
  if (!goal) return;
  try {
    await postOnce('/api/goals/' + goal.id + '/contract/draft', { base_version: goal.current_contract_version, from_fields: true });
    announce('Contract proposed. Review the hash before approving.');
    await refreshAll();
    const found = contracts();
    if (found && found.proposed) await openReview(found.proposed, found.approved);
  } catch (err) { handleWriteError(err); }
}
async function openReview(proposed, approved) {
  const goal = goalRecord();
  if (!goal || !proposed) return;
  $('review-error').textContent = '';
  $('review-hash').textContent = isHash(proposed.body_hash) ? proposed.body_hash : 'Hash not recorded';
  $('review-approve').disabled = true;
  state.review = { id: goal.id, version: proposed.version, body_hash: proposed.body_hash, row_version: Number.isInteger(rowVersion()) ? rowVersion() : goal.row_version };
  $('review-body').replaceChildren(stateBox('loading', 'Loading the field diff…'));
  openDialog('review-dialog', $('review-title'));
  let diff = null;
  let diffError = '';
  try {
    const query = Number.isInteger(approved && approved.version) ? ('from=' + approved.version + '&to=' + proposed.version) : ('to=' + proposed.version);
    diff = await api('/api/goals/' + goal.id + '/contract/diff?' + query);
  } catch (err) { diffError = errorText(err); }
  renderReview(proposed, diff, diffError);
}
function renderReview(proposed, diff, diffError) {
  const parts = [];
  if (diffError) parts.push(banner('wait', 'The comparison did not load', diffError + ' The proposal below is still the record this hash covers. Approval stays disabled until the comparison loads.'));
  else parts.push(renderChanges(diff && diff.changes));
  if (diff && isHash(diff.to_hash) && diff.to_hash !== proposed.body_hash) {
    parts.push(banner('bad', 'The diff hash does not match the proposal', 'Approval is disabled. Reload and review the current hash.'));
  }
  parts.push(el('h3', null, 'Public research frame'), renderFrame(proposed.frame));
  parts.push(el('h3', null, 'Private snapshot'), el('p', { class: 'note' }, 'These values stay in this signed-in session. They are not copied into the shortlist, activity, evidence, or graph.'));
  parts.push(renderSnapshot(proposed.profile_snapshot));
  if (proposed.body && typeof proposed.body === 'object') {
    parts.push(el('h3', null, 'Contract body'), el('pre', { class: 'mono' }, formatValue(proposed.body)));
  }
  $('review-body').replaceChildren(...parts);
  const comparable = !diffError && diff && (!diff.to_hash || diff.to_hash === proposed.body_hash);
  const ready = comparable && isHash(proposed.body_hash) && Number.isInteger(proposed.version) && proposed.profile_snapshot && typeof proposed.profile_snapshot === 'object' && proposed.body && typeof proposed.body === 'object';
  $('review-approve').disabled = !ready || state.stale;
}
function renderChanges(changes) {
  if (!Array.isArray(changes)) return el('p', null, 'Field changes were not returned.');
  if (!changes.length) return el('p', null, 'The server returned no field changes.');
  const body = el('tbody');
  for (const change of changes) {
    body.append(el('tr', null,
      el('td', { class: 'mono' }, change && change.path ? change.path : 'path not recorded'),
      el('td', null, change && change.visibility ? change.visibility : 'visibility not recorded'),
      el('td', null, el('pre', { class: 'mono' }, formatValue(change ? change.before : undefined))),
      el('td', null, el('pre', { class: 'mono' }, formatValue(change ? change.after : undefined)))));
  }
  return el('div', { class: 'tbl-wrap' }, el('table', { class: 'diff' },
    el('thead', null, el('tr', null, ...['Field', 'Visibility', 'Before', 'After'].map((name) => el('th', { scope: 'col' }, name)))), body));
}
function renderSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return el('p', null, 'The private snapshot was not returned.');
  const keys = Object.keys(snapshot).sort();
  if (!keys.length) return el('p', null, 'The private snapshot is empty.');
  return el('div', { class: 'stack' }, ...keys.map((key) => {
    const entry = snapshot[key];
    const value = entry && typeof entry === 'object' && 'value' in entry ? entry.value : entry;
    return el('article', { class: 'snap' },
      el('h4', { class: 'mono' }, key),
      el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, entry && entry.visibility || 'visibility not recorded'), el('span', { class: 'tag' }, entry && entry.status || 'status not recorded'), el('span', { class: 'tag' }, provenanceLabel(entry && entry.provenance))),
      el('pre', { class: 'mono' }, formatValue(value)));
  }));
}
async function approveReviewed() {
  const review = state.review;
  if (!review || state.stale || !isHash(review.body_hash)) return;
  const shown = $('review-hash').textContent.trim();
  if (shown !== review.body_hash) { $('review-error').textContent = 'The displayed hash changed. Reload before approving.'; return; }
  $('review-approve').disabled = true;
  try {
    await postIdempotent('/api/goals/' + review.id + '/contract/approve', {
      version: review.version, body_hash: review.body_hash, expected_row_version: review.row_version,
    });
    closeDialog('review-dialog');
    announce('Contract approved. Research has not been started.');
    await refreshAll();
  } catch (err) {
    if (!handleWriteError(err, $('review-error'))) $('review-approve').disabled = false;
  }
}

function policyOf(contract) {
  return contract && contract.body && contract.body.execution_policy && typeof contract.body.execution_policy === 'object' ? contract.body.execution_policy : null;
}
function renderGrant() {
  const host = $('grant-body');
  const goal = goalRecord();
  if (!goal) { host.replaceChildren(); return; }
  const found = contracts();
  const approved = found && found.approved;
  const grant = state.goal.grant;
  const parts = [el('h3', null, 'Execution grant')];
  if (grant && typeof grant === 'object') parts.push(grantCard(grant));
  else parts.push(el('p', null, 'No active grant is recorded.'));
  if (!approved) { parts.push(el('p', null, 'Approve a contract before granting execution.')); host.replaceChildren(...parts); return; }
  parts.push(grantForm(goal, approved));
  host.replaceChildren(...parts);
}
function grantCard(grant) {
  const lines = ['mode ' + (grant.mode || 'not recorded'), 'state ' + (grant.state || 'not recorded'), 'expires ' + when(grant.expires_at), 'contract version ' + (Number.isInteger(grant.contract_version) ? grant.contract_version : 'not recorded')];
  return el('article', { class: 'snap' }, el('h4', null, 'Active grant'), el('p', null, lines.join(' · ')), renderLimits(grant.limits));
}
function renderLimits(limits) {
  if (!limits || typeof limits !== 'object') return el('p', null, 'Grant limits were not returned.');
  const keys = ['usage_mode', 'day', 'lifetime', 'max_depth', 'max_queue', 'per_domain_inflight', 'max_hypotheses_per_wave', 'exploration_share'];
  return el('pre', { class: 'mono' }, formatValue(Object.fromEntries(keys.filter((key) => key in limits).map((key) => [key, limits[key]]))));
}
function grantForm(goal, approved) {
  const policy = policyOf(approved);
  const ceilings = policy && policy.lifetime_limits && typeof policy.lifetime_limits === 'object' ? policy.lifetime_limits : {};
  const form = el('form', { class: 'stack' });
  const mode = el('select', { id: 'grant-mode' });
  mode.append(el('option', { value: '' }, 'Choose mode'), el('option', { value: 'research' }, 'Research'));
  const monitoring = el('option', { value: 'monitoring' }, 'Monitoring');
  const refresh = approved.body && approved.body.monitoring && approved.body.monitoring.refresh_policy;
  if (!refresh || typeof refresh !== 'object') monitoring.disabled = true;
  mode.append(monitoring);
  const inputs = {};
  function numberField(key, label, cap) {
    const hint = typeof cap === 'number' ? ' (approved ceiling ' + cap + ')' : '';
    const field = textInput(label + hint, '', { inputmode: 'numeric' });
    inputs[key] = field.input;
    return field.node;
  }
  const day = el('fieldset', null, el('legend', null, 'Daily ceilings'), ...['fetches', 'queries', 'model_tokens'].map((key) => numberField('day.' + key, key.replaceAll('_', ' '), ceilings[key])));
  const life = el('fieldset', null, el('legend', null, 'Lifetime ceilings'), ...CHOICES.counters.map((key) => numberField('lifetime.' + key, key.replaceAll('_', ' '), ceilings[key])));
  const structural = el('fieldset', null, el('legend', null, 'Structural limits'), ...CHOICES.structural.map((key) => numberField(key, key.replaceAll('_', ' '), policy && policy[key])));
  const ceilingText = policy ? shareLabel(policy.exploration_share) : '';
  const share = textInput('Exploration share from 0 to 1' + (ceilingText ? ' (approved ceiling ' + ceilingText + ')' : ''), '', {});
  inputs.exploration_share = share.input;
  const expiry = el('input', { type: 'datetime-local' });
  const error = el('p', { class: 'form-error', role: 'alert' });
  const submit = el('button', { class: 'btn', type: 'submit' }, 'Grant execution');
  form.append(el('label', { class: 'field' }, 'Mode', mode), day, life, structural, share.node,
    el('label', { class: 'field' }, 'Expiry', expiry),
    el('p', { class: 'note' }, 'Usage accounting for this grant is estimated. A hard provider-token grant is unavailable. Granting execution does not start research.' + (monitoring.disabled ? ' Monitoring stays unavailable until the approved contract includes a refresh policy.' : '')),
    error, submit);
  form.addEventListener('submit', (ev) => { ev.preventDefault(); submitGrant(goal, approved, mode, inputs, share.input, expiry, error, policy); });
  return form;
}
function submitGrant(goal, approved, mode, inputs, shareInput, expiry, error, policy) {
  error.textContent = '';
  if (mode.value !== 'research' && mode.value !== 'monitoring') { error.textContent = 'Choose research or monitoring.'; return; }
  const day = {}; const life = {};
  for (const key of ['fetches', 'queries', 'model_tokens']) {
    const daily = readInt(inputs['day.' + key], 0, CHOICES.lifetime_cap);
    const lifetime = readInt(inputs['lifetime.' + key], 0, CHOICES.lifetime_cap);
    if (daily === null || lifetime === null) { error.textContent = 'Enter whole-number day and lifetime ceilings for fetches, queries, and model tokens.'; return; }
    if (daily > lifetime) { error.textContent = 'A daily ceiling cannot exceed its lifetime ceiling.'; return; }
    day[key] = daily; life[key] = lifetime;
  }
  for (const key of ['queued_nodes', 'visited_nodes']) {
    const lifetime = readInt(inputs['lifetime.' + key], 0, CHOICES.lifetime_cap);
    if (lifetime === null) { error.textContent = 'Enter lifetime ceilings for queued and visited nodes.'; return; }
    life[key] = lifetime;
  }
  const structural = {};
  for (const key of CHOICES.structural) {
    const cap = CHOICES.structural_caps[key];
    const value = readInt(inputs[key], 0, cap);
    if (value === null) { error.textContent = 'Enter every structural limit as a whole number inside its approved ceiling.'; return; }
    structural[key] = value;
  }
  const share = readShare(shareInput);
  if (share === null) { error.textContent = 'Exploration share must be a number from 0 to 1.'; return; }
  const entered = shareAmount(share);
  if (entered === null) { error.textContent = 'Exploration share must be a number from 0 to 1.'; return; }
  if (policy) {
    const absolute = policy.lifetime_limits || {};
    for (const [key, value] of Object.entries(life)) {
      if (typeof absolute[key] === 'number' && value > absolute[key]) { error.textContent = key.replaceAll('_', ' ') + ' exceeds the approved lifetime ceiling of ' + absolute[key] + '.'; return; }
    }
    for (const key of CHOICES.structural) {
      if (typeof policy[key] === 'number' && structural[key] > policy[key]) { error.textContent = key.replaceAll('_', ' ') + ' exceeds the approved policy.'; return; }
    }
    const ceiling = shareAmount(policy.exploration_share);
    if (ceiling === null) { error.textContent = 'The approved exploration share is not an integer or decimal string. Reload the contract before granting.'; return; }
    if (entered > ceiling) { error.textContent = 'Exploration share exceeds the approved policy.'; return; }
  }
  if (!expiry.value) { error.textContent = 'Choose an expiry date and time.'; return; }
  const ms = new Date(expiry.value).getTime();
  if (!Number.isFinite(ms)) { error.textContent = 'The expiry time is not valid.'; return; }
  const sec = Math.floor(ms / 1000);
  const now = Math.floor(Date.now() / 1000);
  if (sec <= now || sec > now + 366 * 86400) { error.textContent = 'Expiry has to be in the future and within 366 days.'; return; }
  const version = rowVersion();
  if (!Number.isInteger(version)) { error.textContent = 'The goal version is not loaded. Reload before granting.'; return; }
  postIdempotent('/api/goals/' + goal.id + '/grants', {
    contract_version: approved.version, mode: mode.value,
    limits: Object.assign({ usage_mode: 'estimated', day, lifetime: life, exploration_share: share }, structural),
    expires_at: sec, expected_row_version: version,
  }).then(async () => {
    announce('Execution grant recorded. Research has not been started.');
    await refreshAll();
  }).catch((err) => handleWriteError(err, error));
}
function renderControls() {
  const host = $('control-body');
  const goal = goalRecord();
  if (!goal) { host.replaceChildren(); return; }
  const grant = state.goal.grant;
  const reason = el('textarea', { id: 'action-reason', rows: '2', maxlength: '2000' });
  const error = el('p', { id: 'control-error', class: 'form-error', role: 'alert' });
  const specs = controlSpecs(goal, grant, state.goal.inflight_count);
  const buttons = specs.map(([to, label, help]) => {
    const button = el('button', { class: 'btn', type: 'button' }, label);
    button.addEventListener('click', () => (to === 'stop' ? openStop() : runTransition(to, reason, error)));
    return el('div', { class: 'stack' }, button, el('p', { class: 'note' }, help));
  });
  host.replaceChildren(el('h3', null, 'Controls'), el('label', { class: 'field' }, 'Reason for the next action', reason), error, ...buttons);
}
function controlSpecs(goal, grant, inflight) {
  const name = goal.state;
  const active = grant && grant.state === 'active';
  const specs = [];
  if (name === 'approved' && active && grant.mode === 'research') specs.push(['start', 'Start research', 'Start admits work under the active research grant. It is separate from approval.']);
  if (name === 'running' || name === 'monitoring') specs.push(['pause', 'Pause scheduling', 'New work stops being admitted. Already dispatched attempts stay in flight until they settle. The goal is not paused until the server says so.']);
  if (name === 'pausing' && inflight === 0) specs.push(['paused', 'Record paused', 'Available only because the server counts no dispatched work. This does not cancel work that is still running.']);
  if (name === 'paused' && active && grant.mode === 'research') specs.push(['start', 'Resume research', 'Resume continues the same contract and research grant.']);
  if (name === 'paused' && active && grant.mode === 'monitoring') specs.push(['monitoring', 'Resume monitoring', 'Resume continues monitoring. It does not start a research wave.']);
  if (name === 'research_ready' && active && grant.mode === 'monitoring') specs.push(['monitoring', 'Start monitoring', 'Monitoring runs only under an active monitoring grant. Research ready is not an offer.']);
  if (['approved', 'running', 'paused', 'monitoring', 'research_ready'].includes(name)) specs.push(['revise', 'Revise contract', 'The approved contract stays in force until a new version is approved. Previous running work is not resumed automatically.']);
  if (name === 'revising') specs.push(['cancel_revision', 'Cancel revision', 'The draft is discarded. Previous running work returns paused and is not resumed.']);
  if (name === 'running') specs.push(['replan', 'Replan strategy', 'Replanning changes tactics only. The contract and profile stay as approved.']);
  if (name !== 'stopped') specs.push(['stop', 'Stop', 'Stop revokes the active grant and keeps stored evidence.']);
  return specs;
}
function openStop() { $('stop-error').textContent = ''; openDialog('stop-dialog', $('stop-title')); }
async function runTransition(to, reasonInput, error, reasonText) {
  if (error) error.textContent = '';
  const reason = (reasonText !== undefined ? reasonText : reasonInput.value).trim();
  if (!reason) { if (error) error.textContent = 'A reason is required.'; return; }
  const goal = goalRecord();
  const version = rowVersion();
  if (!goal || !Number.isInteger(version)) { if (error) error.textContent = 'The goal version is not loaded. Reload before changing state.'; return; }
  try {
    await postIdempotent('/api/goals/' + goal.id + '/transition', { to, reason, expected_row_version: version });
    if (to === 'stop') closeDialog('stop-dialog');
    await refreshAll();
    const now = goalRecord();
    announce('The goal state is ' + (now ? now.state : 'not loaded') + '.');
  } catch (err) { handleWriteError(err, error); }
}
function renderEvents() {
  const host = $('events-body');
  const events = state.events && Array.isArray(state.events.events) ? state.events.events : null;
  if (!goalRecord()) { host.replaceChildren(); return; }
  if (!events) { host.replaceChildren(el('h3', null, 'Change history'), el('p', null, 'Change history was not returned.')); return; }
  if (!events.length) { host.replaceChildren(el('h3', null, 'Change history'), el('p', null, 'No goal events are recorded yet.')); return; }
  const shown = events.slice(-50);
  host.replaceChildren(el('h3', null, 'Change history'), events.length > 50 ? el('p', { class: 'note' }, 'Showing the latest 50 of ' + events.length + ' recorded events.') : null,
    ...shown.map((event) => el('article', { class: 'snap' },
      el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, event.operation || 'event'), el('span', null, when(event.at)), el('span', { class: 'mono' }, event.actor || '')),
      el('p', null, (event.from_state || 'none') + ' → ' + (event.to_state || 'none')),
      el('p', null, event.reason ? event.reason : 'Reason not recorded.'),
      event.version !== null && event.version !== undefined ? el('p', { class: 'mono' }, 'Version ' + event.version) : null)));
}
function renderGoal() {
  const goal = goalRecord();
  const badge = $('goal-badge');
  if (!goal) {
    badge.textContent = '';
    for (const id of ['interview-body', 'contract-body', 'grant-body', 'control-body', 'events-body']) $(id).replaceChildren();
    renderCreate();
    renderGoalBanners();
    return;
  }
  $('goal-create').replaceChildren();
  badge.textContent = labelState(goal.state);
  renderGoalBanners();
  renderInterview();
  renderContract();
  renderGrant();
  renderControls();
  renderEvents();
}
function renderCreate() {
  const host = $('goal-create');
  if (goalRecord() || !state.csrf) { host.replaceChildren(); return; }
  const title = el('input', { id: 'goal-title', type: 'text', maxlength: '300' });
  const error = el('p', { class: 'form-error', role: 'alert' });
  const form = el('form', { class: 'stack' }, el('h3', null, 'Create a goal'),
    el('p', { class: 'note' }, 'The server assigns the identifier. A non-stopped goal cannot be created twice.'),
    el('label', { class: 'field' }, 'Title', title), error, el('button', { class: 'btn', type: 'submit' }, 'Create goal'));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    error.textContent = '';
    const value = title.value.trim();
    if (!value) { error.textContent = 'Enter a title.'; return; }
    try { await postOnce('/api/goals', { title: value }); announce('Goal created.'); await refreshAll(); }
    catch (err) {
      if (err.status === 409) { error.textContent = 'A goal that is not stopped already exists. Reload it instead of creating another.'; return; }
      handleWriteError(err, error);
    }
  });
  host.replaceChildren(form);
}
function renderGoalBanners() {
  const host = $('goal-banners');
  const goal = goalRecord();
  if (!state.csrf) { host.replaceChildren(signedOutCopy()); return; }
  if (!goal) { host.replaceChildren(stateBox('empty', 'No goal is recorded', 'Create one to start the interview. Nothing is prefilled.')); return; }
  const nodes = [];
  const inflight = state.goal.inflight_count;
  if (goal.state === 'pausing') {
    const count = typeof inflight === 'number' ? inflight : null;
    nodes.push(banner('wait', 'Pausing', count === null ? 'In-flight work is still being counted. This is not paused.' : count + ' dispatched attempt' + (count === 1 ? '' : 's') + ' still in flight. Pause does not mean the work has already stopped.'));
  } else if (goal.state === 'paused') nodes.push(banner('info', 'Paused', 'New work is not admitted. Resume is a separate action.'));
  else if (goal.state === 'stopped') nodes.push(banner('info', 'Stopped', goal.stop_reason ? 'Reason: ' + goal.stop_reason : 'No stop reason was recorded.'));
  else if (goal.state === 'research_ready') nodes.push(banner('ok', 'Research ready is not an offer', 'The completion rule was met. This is not an admission, a visa, or received funding.'));
  const blocker = state.goal.blocker;
  if (blocker && !(blocker === 'goal_paused' && (goal.state === 'pausing' || goal.state === 'paused' || goal.state === 'revising'))) nodes.push(banner(blocker === 'grant_exhausted' || blocker === 'grant_expired' ? 'bad' : 'info', 'Work is not being admitted', blockerText(blocker)));
  const completion = state.goal.completion;
  if (completion && completion.ready === true && goal.state !== 'research_ready') nodes.push(banner('wait', 'Completion reports ready', 'The goal state is still ' + labelState(goal.state) + '. Ready is not an offer.'));
  if (completion) nodes.push(el('p', { class: 'note' }, completionLine(completion)));
  if (state.goal.substate) nodes.push(el('p', { class: 'note' }, 'Substate: ' + state.goal.substate + (state.goal.substate_reason ? '. ' + state.goal.substate_reason : '.')));
  host.replaceChildren(...nodes);
}
function blockerText(code) {
  const copy = {
    no_goal: 'No goal is recorded.', goal_paused: 'The goal is paused, pausing, or being revised.',
    research_ready: 'Research ready does not admit another research wave.', not_running: 'The goal is not running.',
    no_grant: 'No active execution grant matches the current contract.', grant_expired: 'The execution grant is past its expiry.',
    grant_exhausted: 'The execution grant is exhausted. Nothing is described as within budget.',
    mode_mismatch: 'The active grant mode does not match the goal.', no_contract: 'The current contract is not approved.',
  };
  return copy[code] || code;
}
function completionLine(completion) {
  const bits = ['Completion ready: ' + (completion.ready === true ? 'yes' : completion.ready === false ? 'no' : 'not recorded')];
  if (completion.substate) bits.push('substate ' + completion.substate);
  const detail = completion.detail;
  if (typeof detail === 'string') bits.push(detail);
  else if (detail && typeof detail === 'object') {
    for (const key of ['message', 'matched', 'required', 'checked', 'reason']) if (detail[key] !== undefined && typeof detail[key] !== 'object') bits.push(key + ' ' + detail[key]);
  }
  return bits.join('. ');
}

function renderShortlist() {
  const banners = $('short-banners');
  const host = $('short-body');
  if (!state.csrf) { banners.replaceChildren(); host.replaceChildren(signedOutCopy()); return; }
  if (!goalRecord()) { banners.replaceChildren(); host.replaceChildren(stateBox('empty', 'No goal is recorded', 'A shortlist appears after a goal exists and programs are linked.')); return; }
  const data = state.shortlist;
  if (!data) { banners.replaceChildren(); host.replaceChildren(stateBox('empty', 'Shortlist is not loaded', 'Refresh to read constraint results.')); return; }
  const notes = [banner('info', 'How to read a result', 'Met and unmet are evidence judgments. Unknown is not met. Conditional is a documented contingency, not an award. Contested means the sources conflict and stay unresolved.')];
  if (data.reviewer_available === false) notes.push(banner('bad', 'Reviewer unavailable', 'Pending observations stay unqualified. Nothing here is treated as reviewed.'));
  else if (data.reviewer_available !== true) notes.push(banner('wait', 'Reviewer availability was not reported', 'Do not treat missing review status as a completed review.'));
  if (data.outcome === 'no_match' || (data.completion && data.completion.substate === 'no_match')) notes.push(banner('info', 'No match among checked programs', 'Checked programs did not satisfy the approved criteria. This is not an empty database.'));
  if (data.completion && data.completion.ready === true) notes.push(banner('ok', 'Research ready is not an offer', 'A completed shortlist is not admission, a visa, or received funding.'));
  if (data.completion) notes.push(el('p', { class: 'note' }, completionLine(data.completion)));
  banners.replaceChildren(...notes);
  const programs = data.programs;
  if (!Array.isArray(programs)) { host.replaceChildren(stateBox('error', 'Programs were not returned', 'The shortlist cannot be shown.')); return; }
  if (!programs.length) {
    const noMatch = data.outcome === 'no_match';
    host.replaceChildren(stateBox('empty', noMatch ? 'No match among checked programs' : 'Nothing has been evaluated yet', noMatch ? 'The completion check found no qualifying program among those already checked.' : 'No programs are linked to the current contract. This is not a no-match result.'));
    return;
  }
  host.replaceChildren(el('div', { id: 'matrix' }, ...programs.map(programCard)));
  wireMatrix();
}
function programCard(program) {
  const packages = Array.isArray(program.packages) ? program.packages : null;
  const matrix = Array.isArray(program.matrix) ? program.matrix : null;
  const evaluated = program.evaluated === true ? 'Evaluated' : program.evaluated === false ? 'Not evaluated' : 'Evaluation flag not recorded';
  return el('article', { class: 'program' },
    el('h3', null, program.name || program.program_key || 'Program name not recorded'),
    el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, program.role || 'role not recorded'), program.program_key ? el('span', { class: 'mono' }, program.program_key) : null, el('span', { class: 'tag' }, evaluated)),
    packages === null ? el('p', null, 'Funding packages were not returned.') : packages.length ? packageList(packages) : el('p', null, 'No funding package is recorded for this program.'),
    matrix === null ? el('p', null, 'Constraint results were not returned.') : matrix.length ? matrixGroups(program, matrix) : el('p', null, 'No constraint results are recorded.'));
}
function packageList(packages) {
  return el('div', { class: 'stack' }, ...packages.map((pkg) => el('section', null,
    el('h4', null, pkg.cycle_label ? 'Cycle ' + pkg.cycle_label : 'Intake cycle unknown'),
    el('p', { class: 'note' }, pkg.cycle_label ? 'The cycle label is what the source recorded. It is not an open deadline by itself.' : 'An undated or unlabeled cycle is not an open deadline.'),
    el('p', { class: 'mono' }, pkg.key || 'Package key not recorded'), componentsOf(pkg))));
}
function componentsOf(pkg) {
  const parts = pkg.components;
  if (!Array.isArray(parts)) return el('p', null, 'Package components were not returned.');
  if (!parts.length) return el('p', null, 'No component amounts are recorded.');
  return el('ul', null, ...parts.map((part) => el('li', null, (part.label || 'Component') + ': ' + (part.amount === null || part.amount === undefined ? 'amount not recorded' : part.amount) + (part.currency ? ' ' + part.currency : '') + (part.period ? ' · ' + part.period : ' · period unknown'))));
}
function matrixGroups(program, matrix) {
  const groups = new Map();
  for (const cell of matrix) {
    const key = cell.package_key || 'Package key not recorded';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(cell);
  }
  return el('div', { class: 'stack' }, ...[...groups].map(([key, cells]) => el('section', null,
    el('h4', { class: 'mono' }, key), el('div', { class: 'cells', role: 'group', 'aria-label': 'Constraints for ' + key }, ...cells.map((cell, index) => cellButton(program, cell, index))))));
}
function cellButton(program, cell, index) {
  const status = typeof cell.status === 'string' ? cell.status : '';
  const known = Object.prototype.hasOwnProperty.call(STATUS_LABEL, status);
  const label = known ? STATUS_LABEL[status] : (status ? 'Unrecognized status: ' + status : 'Status not recorded');
  const button = el('button', { type: 'button', class: 'cell mark ' + (known ? status : 'other'), tabindex: '0', 'aria-label': (program.name || program.program_key || 'Program') + ', ' + (cell.constraint_key || 'constraint') + ', ' + label + '. Open evidence.' }, label);
  button.addEventListener('click', () => openEvidence(program.program_key, cell.constraint_key));
  const notes = [];
  if (cell.reason) notes.push(cell.reason);
  if (status === 'conditional') notes.push('Not an award.');
  if (status === 'contested') notes.push('Unresolved. Not a passing result.');
  if (status === 'unknown' && cell.needs_human_review) notes.push('Needs human review.');
  if (cell.next && cell.next.label) notes.push('Next: ' + cell.next.label);
  return el('div', { class: 'cell-wrap' }, el('span', { class: 'cell-key' }, cell.constraint_key || 'Constraint key not recorded'), button, notes.length ? el('span', { class: 'cell-note' }, notes.join(' ')) : null);
}
function wireMatrix() {
  const root = $('matrix');
  if (!root) return;
  const buttons = [...root.querySelectorAll('button.cell')];
  buttons.forEach((button, index) => { button.tabIndex = index === 0 ? 0 : -1; });
  buttons.forEach((button, index) => {
    button.addEventListener('keydown', (ev) => {
      let next = null;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') next = (index + 1) % buttons.length;
      else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length;
      else if (ev.key === 'Home') next = 0;
      else if (ev.key === 'End') next = buttons.length - 1;
      else return;
      ev.preventDefault();
      buttons.forEach((item) => { item.tabIndex = -1; });
      buttons[next].tabIndex = 0;
      buttons[next].focus();
    });
  });
}
function renderActivity() {
  const banners = $('activity-banners');
  const host = $('activity-body');
  if (!state.csrf) { banners.replaceChildren(); host.replaceChildren(signedOutCopy()); return; }
  if (!goalRecord()) { banners.replaceChildren(); host.replaceChildren(stateBox('empty', 'No goal is recorded', 'Activity is available after a goal exists.')); return; }
  const data = state.activity;
  if (!data) { banners.replaceChildren(); host.replaceChildren(stateBox('empty', 'Activity is not loaded', 'Refresh to read tasks, decisions, and the ledger.')); return; }
  banners.replaceChildren(...activityBanners(data));
  host.replaceChildren(taskSection(data), ledgerSection(data.ledger, data.resets_at), decisionSection(data.decisions), engineSection(data.engines), coverageSection(data.coverage), strategySection(data.strategy));
}
function activityBanners(data) {
  const nodes = [];
  const sub = data.substate;
  const count = typeof data.inflight_count === 'number' ? data.inflight_count : null;
  if (data.state === 'pausing' || sub === 'pausing') nodes.push(banner('wait', 'Pausing', (count === null ? 'Dispatched work is still being counted.' : count + ' dispatched attempt' + (count === 1 ? ' is' : 's are') + ' still in flight.') + ' The task list below is queued, leased, or paused work, not a claim that pause has finished.'));
  else if (data.state === 'paused') nodes.push(banner('info', 'Paused', 'The server records this goal as paused.'));
  if (sub === 'blocked') nodes.push(banner('bad', 'Research is blocked', data.reason || 'No reason was recorded.'));
  if (sub === 'stalled') nodes.push(banner('wait', 'Research is stalled', data.reason || 'The same work repeated without new supported evidence.'));
  if (sub === 'outage') nodes.push(banner('bad', 'Outage', data.reason || 'A source or engine outage is recorded. Work is not reported as finished.'));
  if (sub === 'exhausted' || data.grant_state === 'exhausted') nodes.push(banner('bad', 'Exhausted', 'A recorded ceiling was reached. Nothing is described as within budget. ' + (typeof data.resets_at === 'number' ? 'Next daily window: ' + fmtTime(data.resets_at) + '. A lifetime ceiling does not reset then.' : 'No daily reset time was recorded.')));
  if (sub === 'verifier_unavailable') nodes.push(banner('bad', 'Reviewer unavailable', 'Observations stay pending and are not qualified.'));
  if (sub === 'empty_frontier') nodes.push(banner('info', 'The frontier is empty', 'No queued public work is recorded for this strategy.'));
  if (sub === 'partial_coverage') nodes.push(banner('wait', 'Coverage is partial', data.reason || 'The checked programs do not yet meet the completion rule.'));
  if (data.reason && !nodes.some((node) => node.textContent && node.textContent.includes(data.reason))) nodes.push(el('p', { class: 'note' }, 'Recorded reason: ' + data.reason));
  return nodes;
}
function taskSection(data) {
  const tasks = data.tasks;
  const head = el('h3', null, 'Queued and leased tasks');
  if (!Array.isArray(tasks)) return el('section', null, head, el('p', null, 'Tasks were not returned.'));
  const count = typeof data.inflight_count === 'number' ? data.inflight_count : null;
  const note = el('p', { class: 'note' }, (count === null ? 'Dispatched in-flight count was not returned.' : 'Dispatched in-flight count: ' + count + '.') + ' That count is separate from the rows below.');
  if (!tasks.length) return el('section', null, head, note, el('p', null, 'No queued, leased, or paused tasks are recorded.'));
  return el('section', null, head, note, el('div', { class: 'tbl-wrap' }, el('table', null,
    el('thead', null, el('tr', null, ...['Task', 'Lease', 'Phase', 'Domain', 'Node', 'Lease until'].map((name) => el('th', { scope: 'col' }, name)))),
    el('tbody', null, ...tasks.map((task) => el('tr', null,
      el('td', { class: 'mono' }, task.task_id === undefined || task.task_id === null ? 'not recorded' : String(task.task_id)),
      el('td', null, task.lease_state || 'not recorded'), el('td', null, task.phase || 'not recorded'),
      el('td', null, task.domain || 'not recorded'), el('td', null, task.node_key || 'not recorded'),
      el('td', null, typeof task.lease_until === 'number' ? fmtTime(task.lease_until) + (task.lease_until * 1000 < Date.now() ? ' (lease time has passed)' : '') : 'not recorded')))))));
}
function ledgerSection(ledger, resets) {
  const head = el('h3', null, 'Ledger headroom');
  if (ledger === null || ledger === undefined) return el('section', null, head, el('p', null, 'No ledger headroom was returned.'));
  if (typeof ledger !== 'object') return el('section', null, head, el('p', null, 'Ledger headroom was not readable.'));
  const blocks = ['day', 'lifetime'].map((kind) => {
    const window = ledger[kind];
    if (!window || typeof window !== 'object' || !window.counters || typeof window.counters !== 'object') return el('p', null, kind + ' headroom was not returned.');
    const rows = Object.entries(window.counters).map(([counter, row]) => gauge(counter, row || {}));
    return el('section', null, el('h4', null, kind + (window.window_key ? ' · ' + window.window_key : '')), ...rows);
  });
  return el('section', null, head, typeof resets === 'number' ? el('p', { class: 'note' }, 'Next daily window: ' + fmtTime(resets) + '. Lifetime counters do not reset with that window.') : null, ...blocks);
}
function gauge(counter, row) {
  const used = typeof row.used === 'number' ? row.used : null;
  const ceiling = typeof row.ceiling === 'number' ? row.ceiling : null;
  const remaining = typeof row.remaining === 'number' ? row.remaining : null;
  const pct = used !== null && ceiling ? Math.max(0, Math.min(100, used / ceiling * 100)) : 0;
  return el('div', { class: 'gauge' }, el('div', { class: 'gauge-label' }, el('span', null, counter), el('span', { class: 'mono' }, (used === null ? 'used not recorded' : 'used ' + fmtNum(used)) + (ceiling === null ? '' : ' of ' + fmtNum(ceiling)) + (remaining === null ? '' : ', remaining ' + fmtNum(remaining)))),
    ceiling ? el('div', { class: 'bar', role: 'presentation' }, el('span', { style: 'width:' + pct + '%' })) : null);
}
function decisionSection(decisions) {
  const head = el('h3', null, 'Frontier decisions');
  if (!Array.isArray(decisions)) return el('section', null, head, el('p', null, 'Decisions were not returned.'));
  if (!decisions.length) return el('section', null, head, stateBox('empty', 'No frontier decisions are recorded', 'An empty decision list is not a claim that research finished.'));
  return el('section', null, head, ...decisions.map((item) => el('article', { class: 'snap' },
    el('p', { class: 'meta-line' }, el('span', null, when(item.at)), el('span', { class: 'mono' }, item.node_key || 'node not recorded'), item.priority !== null && item.priority !== undefined ? el('span', null, 'priority ' + item.priority) : null),
    el('p', null, item.reason || 'Reason not recorded.'), item.deferred_until ? el('p', null, 'Deferred until ' + when(item.deferred_until)) : null)));
}
function engineSection(engines) {
  const head = el('h3', null, 'Engines');
  if (!Array.isArray(engines)) return el('section', null, head, el('p', null, 'Engine status was not returned.'));
  if (!engines.length) return el('section', null, head, el('p', null, 'No engine status was recorded.'));
  const soft = engines.some((engine) => ['estimated', 'unknown', 'legacy'].includes(engine.usage_kind));
  return el('section', null, head, soft ? el('p', { class: 'note' }, 'Estimated, unknown, or legacy usage is not a hard spending cap.') : null,
    el('ul', null, ...engines.map((engine) => el('li', null, (engine.role || 'role not recorded') + ': ' + (engine.available === true ? 'available' : engine.available === false ? 'unavailable' : 'availability not recorded') + (engine.model ? ' · ' + engine.model : '') + ' · usage ' + (engine.usage_kind || 'not recorded') + (engine.reason ? ' · ' + engine.reason : '')))));
}
function coverageSection(coverage) {
  const head = el('h3', null, 'Coverage');
  if (coverage === null || coverage === undefined) return el('section', null, head, el('p', null, 'Coverage has not been recorded.'));
  if (typeof coverage !== 'object') return el('section', null, head, el('p', null, 'Coverage was not readable.'));
  return el('section', null, head, el('p', null, 'Programs ' + (typeof coverage.program_count === 'number' ? coverage.program_count : 'not recorded') + '. Unknown fields ' + (typeof coverage.unknown_fields === 'number' ? coverage.unknown_fields : 'not recorded') + '. Contested fields ' + (typeof coverage.contested_fields === 'number' ? coverage.contested_fields : 'not recorded') + '.'));
}
function strategySection(strategy) {
  const head = el('h3', null, 'Strategy');
  if (strategy === null || strategy === undefined) return el('section', null, head, el('p', null, 'No strategy outcome was recorded.'));
  if (typeof strategy !== 'object') return el('section', null, head, el('p', null, 'Strategy was not readable.'));
  return el('section', null, head, el('p', null, 'Version ' + (strategy.version === undefined || strategy.version === null ? 'not recorded' : strategy.version) + '. Outcome ' + (strategy.outcome || 'not recorded') + '.'), el('p', null, strategy.reason || 'Reason not recorded.'));
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(String(value));
  return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}
function programName(key) {
  const programs = state.shortlist && state.shortlist.programs;
  if (!Array.isArray(programs)) return key;
  const found = programs.find((item) => item.program_key === key);
  return found && found.name || key;
}
async function openEvidence(programKey, fieldKey) {
  if (!programKey) { announce('This cell has no program key, so evidence cannot be opened.'); return; }
  state.evidenceKey = programKey;
  state.evidenceField = fieldKey || '';
  selectTab('evidence', false);
  const heading = $('evidence-heading');
  if (heading) heading.focus();
  await loadEvidence();
}
async function loadEvidence() {
  const host = $('evidence-body');
  const banners = $('evidence-banners');
  if (!state.csrf) { banners.replaceChildren(); host.replaceChildren(signedOutCopy()); return; }
  const id = goalId();
  if (!state.evidenceKey || id === null) {
    banners.replaceChildren();
    host.replaceChildren(stateBox('empty', 'No program is selected', 'Choose a constraint cell on the shortlist. Enter opens its evidence.'));
    return;
  }
  host.replaceChildren(stateBox('loading', 'Loading evidence…'));
  try {
    const data = await api('/api/goals/' + id + '/evidence/' + encodeURIComponent(state.evidenceKey));
    state.evidence = data;
    renderEvidence(data);
    if (state.evidenceField) {
      const target = host.querySelector('[data-field="' + cssEscape(state.evidenceField) + '"]');
      if (target) target.focus();
    }
  } catch (err) {
    if (err.status === 401) return;
    banners.replaceChildren();
    host.replaceChildren(stateBox('error', err.status === 404 ? 'No evidence is recorded for this program' : 'Could not load evidence', errorText(err), loadEvidence));
  }
}
function renderEvidence(data) {
  const banners = $('evidence-banners');
  const host = $('evidence-body');
  if (!data || typeof data !== 'object') { banners.replaceChildren(); host.replaceChildren(stateBox('error', 'Evidence was not readable', 'The response did not include an evidence record.')); return; }
  const observations = Array.isArray(data.observations) ? data.observations : null;
  const pending = data.review_pending === true || (observations || []).some((item) => item.assurance === 'review_pending' || item.assurance === 'review_unavailable');
  const notes = [];
  if (pending) notes.push(banner('wait', 'Review is pending or unavailable', 'Pending observations are not qualified evidence.'));
  if (data.review_pending === false && !pending) notes.push(el('p', { class: 'note' }, 'The server did not mark this program review-pending. Individual assurance labels below still govern each quote.'));
  banners.replaceChildren(...notes);
  const title = el('h3', null, programName(data.program_key || state.evidenceKey));
  host.replaceChildren(title,
    observations === null ? el('p', null, 'Observations were not returned.') : observationList(observations),
    contradictionList(data.contradictions), legacyList(data.legacy_leads), noteList(data.human_notes));
}
function observationList(observations) {
  if (!observations.length) return stateBox('empty', 'No observations are recorded', 'An empty observation list is not a qualification.');
  const groups = new Map();
  for (const item of observations) {
    const key = item.field_key || 'Field not recorded';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return el('div', { class: 'stack' }, ...[...groups].map(([key, items]) => el('section', { class: 'stack', tabindex: '-1', 'data-field': key },
    el('h3', null, key), ...items.map(observationCard))));
}
function observationCard(item) {
  const caption = [el('span', { class: 'tag' }, assuranceLabel(item.assurance)), el('span', null, 'Snapshot ' + when(item.snapshot_at)), el('span', null, 'Reviewer ' + (item.reviewer_model || 'not recorded')), el('span', null, item.cycle_label ? 'Cycle ' + item.cycle_label : 'Cycle applicability unknown'), el('span', null, item.period ? 'Period ' + item.period : 'Period unknown')];
  if (!item.cycle_label) caption.push(el('span', null, 'An undated source is not an open deadline.'));
  return el('figure', { class: 'evidence' }, el('span', { class: 'label' }, 'Source quote'),
    el('blockquote', null, item.quote ? item.quote : 'No quote was stored for this observation.'),
    el('figcaption', null, ...caption, item.source_title ? el('span', null, item.source_title) : null, linkOrMissing(item.source_url)));
}
function linkOrMissing(url) { return url ? linkTo(url, url) : el('span', null, 'Source link not recorded'); }
function contradictionList(items) {
  const head = el('h3', null, 'Contradictions');
  if (!Array.isArray(items)) return el('section', null, head, el('p', null, 'Contradictions were not returned.'));
  if (!items.length) return el('section', null, head, el('p', null, 'No contradictions are recorded for this program.'));
  return el('section', { class: 'stack' }, head, ...items.map((item) => el('article', { class: 'snap', tabindex: '-1', 'data-field': item.field_key || '' },
    el('h4', null, item.field_key || 'Field not recorded'),
    el('p', null, item.unresolved ? 'Unresolved. Qualification stays unknown until a person reviews it.' : (item.resolution || 'A resolution was recorded without reason text.')),
    el('blockquote', null, item.quote_a || 'First quote was not stored.'), el('blockquote', null, item.quote_b || 'Second quote was not stored.'))));
}
function legacyList(items) {
  const head = el('h3', null, 'Historical leads');
  if (!Array.isArray(items)) return el('section', null, head, el('p', null, 'Historical leads were not returned.'));
  if (!items.length) return el('section', null, head, el('p', null, 'No historical leads are attached to this program. Historical leads are not funding or qualification evidence.'));
  return el('section', { class: 'stack' }, head, el('p', { class: 'note' }, 'Historical leads are not counted as funding or qualification evidence.'), ...items.map((item) => el('article', { class: 'snap' },
    el('h4', null, item.name || 'Lead name not recorded'), el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, item.provenance || 'provenance not recorded')),
    item.quote ? el('blockquote', null, item.quote) : el('p', null, 'No quote was stored.'), linkOrMissing(item.source_url))));
}
function noteList(items) {
  const head = el('h3', null, 'Human notes');
  const rule = el('p', { class: 'note' }, 'A human note is an annotation, not source evidence.');
  if (!Array.isArray(items)) return el('section', null, head, rule, el('p', null, 'Human notes were not returned.'));
  if (!items.length) return el('section', null, head, rule, el('p', null, 'No human notes are attached.'));
  return el('section', { class: 'stack' }, head, rule, ...items.map((item) => el('article', { class: 'snap' },
    el('p', { class: 'meta-line' }, el('span', { class: 'tag' }, 'Human note'), el('span', null, item.provenance || 'provenance not recorded')),
    el('p', null, item.text || 'Note text was not stored.'))));
}
function renderGraphBanner() {
  const host = $('graph-banners');
  const sub = state.activity && state.activity.substate;
  if (sub === 'blocked') host.replaceChildren(banner('bad', 'Research is blocked', (state.activity.reason || 'No reason was recorded.') + ' The map below is the public evidence graph, not the goal frontier.'));
  else host.replaceChildren();
}
async function loadGraph() {
  setOverlay(stateBox('loading', 'Loading knowledge graph…'));
  setGraphMsg('');
  try {
    const data = await api('/api/graph');
    if (!data || !Array.isArray(data.nodes) || !Array.isArray(data.links)) {
      throw new ApiError(0, { error: 'shape', message: 'The graph response did not include nodes and links.' });
    }
    initGraph(data);
  } catch (err) {
    if (err.status === 401) return;
    graphToken += 1;
    state.g = null;
    const svg = $('graph-svg'); if (svg) svg.replaceChildren();
    const blocked = !err.status || err.status === 503 || err.status === 429;
    setOverlay(stateBox('error', blocked ? 'The knowledge graph is unavailable' : 'Could not load the knowledge graph', (blocked ? errorText(err) + ' This is not an empty graph.' : errorText(err)), loadGraph));
  }
}
async function loadStatus() {
  try {
    const status = await api('/api/status');
    state.status = status && typeof status === 'object' ? status : null;
    if (status && typeof status.database === 'string' && status.database) $('db-name').textContent = status.database;
  } catch (_) { /* The database name is optional. A failure here is not an empty graph. */ }
}
async function loadGoal() {
  const data = await api('/api/goal');
  if (!data || !data.goal || typeof data.goal.id !== 'number') throw new ApiError(0, { error: 'shape', message: 'The goal response was incomplete.' });
  state.goal = data;
  return data;
}
async function refreshAll() {
  if (state.loading) { state.refreshQueued = true; return; }
  state.loading = true;
  state.refreshQueued = false;
  const button = $('refresh'); if (button) button.disabled = true;
  showTop('');
  if (!$('review-dialog').hidden) closeDialog('review-dialog');
  try {
    if (!state.csrf) {
      try {
        const session = await api('/api/session');
        if (!takeSession(session)) {
          showTop('The session response did not include an authenticated session, absolute expiry, and CSRF token.');
        }
      } catch (err) {
        if (err.status !== 401) {
          showTop(errorText(err));
          renderSignedOut();
          await loadGraph();
          return;
        }
      }
    }
    if (!state.csrf) {
      clearPrivate();
      $('signin').hidden = false;
      $('signout').hidden = true;
      renderSignedOut();
      await Promise.all([loadGraph(), loadStatus()]);
      return;
    }
    let missing = false;
    try { await loadGoal(); }
    catch (err) {
      if (err.status === 404) { state.goal = null; missing = true; }
      else throw err;
    }
    if (missing) {
      state.interview = null; state.events = null; state.shortlist = null; state.activity = null;
      state.evidenceKey = ''; state.evidenceField = ''; state.evidence = null;
    } else {
      const id = goalId();
      const results = await Promise.allSettled([
        api('/api/goals/' + id + '/interview'), api('/api/goals/' + id + '/events'),
        api('/api/goals/' + id + '/shortlist'), api('/api/goals/' + id + '/activity'),
      ]);
      const denied = results.find((item) => item.status === 'rejected' && item.reason && item.reason.status === 401);
      if (denied) return;
      state.interview = results[0].status === 'fulfilled' ? results[0].value : null;
      state.events = results[1].status === 'fulfilled' ? results[1].value : null;
      state.shortlist = results[2].status === 'fulfilled' ? results[2].value : null;
      state.activity = results[3].status === 'fulfilled' ? results[3].value : null;
      const failed = results.find((item) => item.status === 'rejected');
      if (failed) showTop(errorText(failed.reason));
    }
    renderGoal();
    renderShortlist();
    renderActivity();
    renderGraphBanner();
    if (state.evidenceKey) await loadEvidence();
    else {
      $('evidence-banners').replaceChildren();
      $('evidence-body').replaceChildren(stateBox('empty', 'No program is selected', 'Choose a constraint cell on the shortlist. Enter opens its evidence.'));
    }
    await Promise.all([loadGraph(), loadStatus()]);
    state.stale = false;
    replays.clear();
    $('signout').hidden = false;
    $('signin').hidden = true;
    $('refreshed').textContent = 'signed in until ' + fmtTime(state.sessionExpires) + ' · refreshed ' + new Date().toLocaleTimeString();
    if (!$('stale-dialog').hidden) closeDialog('stale-dialog');
    announce('Console updated.');
  } catch (err) {
    if (err.status !== 401) showTop(errorText(err));
  } finally {
    state.loading = false;
    if (button) button.disabled = false;
    if (state.refreshQueued) { state.refreshQueued = false; await refreshAll(); }
  }
}
function renderSignedOut() {
  $('goal-badge').textContent = '';
  $('goal-banners').replaceChildren(signedOutCopy());
  for (const id of ['goal-create', 'interview-body', 'contract-body', 'grant-body', 'control-body', 'events-body', 'short-banners', 'activity-banners', 'evidence-banners']) $(id).replaceChildren();
  $('short-body').replaceChildren(signedOutCopy());
  $('activity-body').replaceChildren(signedOutCopy());
  $('evidence-body').replaceChildren(signedOutCopy());
}
function selectTab(name, focus) {
  if (!TAB_IDS.includes(name)) name = 'goal';
  for (const id of TAB_IDS) {
    const on = id === name;
    const tab = $('tab-' + id);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    $('panel-' + id).hidden = !on;
  }
  if (focus) $('tab-' + name).focus();
  if (history.replaceState) history.replaceState(null, '', '#' + name);
  if (name === 'graph') { resizeStage(); if (state.g && state.g.needsFit) fitView(); }
}
function initTabs() {
  const tabs = TAB_IDS.map((id) => $('tab-' + id));
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(TAB_IDS[index], false));
    tab.addEventListener('keydown', (ev) => {
      let next = null;
      if (ev.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (ev.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      else if (ev.key === 'Home') next = 0;
      else if (ev.key === 'End') next = tabs.length - 1;
      else return;
      ev.preventDefault();
      selectTab(TAB_IDS[next], true);
    });
  });
  const raw = (location.hash || '').replace(/^#/, '');
  selectTab(TAB_IDS.includes(raw) ? raw : 'goal', false);
}
async function login(ev) {
  ev.preventDefault();
  const input = $('login-token');
  const token = input.value;
  input.value = '';
  $('login-error').textContent = '';
  if (!token.trim()) { $('login-error').textContent = 'Enter the sign-in token.'; return; }
  try {
    const body = await api('/api/session', { method: 'POST', body: { token } });
    if (!takeLogin(body)) { $('login-error').textContent = 'Sign-in did not return a CSRF token and an absolute expiry.'; return; }
    closeDialog('login-dialog');
    announce('Signed in.');
    await refreshAll();
  } catch (err) {
    let msg = safeMessage(err, token);
    if (err.status === 401) msg += ' The token was not stored. It cannot be used again; reload uses the session cookie.';
    $('login-error').textContent = msg;
  }
}
async function logout() {
  try {
    await api('/api/session', { method: 'DELETE', body: {} });
  } catch (err) {
    showTop(errorText(err) + ' The session was not ended.');
    return;
  }
  dropSession();
  announce('Signed out.');
  await loadGraph();
}
async function boot() {
  initTabs();
  initGraphInteractions();
  try {
    const session = await api('/api/session');
    if (!takeSession(session)) {
      throw new ApiError(0, { error: 'shape', message: 'The session response did not include an authenticated session, absolute expiry, and CSRF token.' });
    }
    await refreshAll();
  } catch (err) {
    if (err.status === 401) {
      dropSession();
      openDialog('login-dialog', $('login-token'));
    } else {
      showTop(errorText(err));
      renderSignedOut();
    }
    await loadGraph();
  }
}
function bindStatic() {
  $('login-form').addEventListener('submit', login);
  $('login-close').addEventListener('click', () => closeDialog('login-dialog'));
  $('review-close').addEventListener('click', () => closeDialog('review-dialog'));
  $('review-approve').addEventListener('click', approveReviewed);
  $('stale-close').addEventListener('click', () => closeDialog('stale-dialog'));
  $('stale-reload').addEventListener('click', async () => { closeDialog('stale-dialog'); await refreshAll(); });
  $('stop-close').addEventListener('click', () => closeDialog('stop-dialog'));
  $('stop-confirm').addEventListener('click', () => runTransition('stop', null, $('stop-error'), $('stop-reason').value));
  $('signin').addEventListener('click', () => openDialog('login-dialog', $('login-token')));
  $('signout').addEventListener('click', logout);
  $('refresh').addEventListener('click', refreshAll);
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== '/' || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const target = ev.target;
    if (target && (target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
    if ($('panel-graph').hidden) return;
    ev.preventDefault();
    $('g-q').focus();
  });
}
bindStatic();
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();

})();
"""
