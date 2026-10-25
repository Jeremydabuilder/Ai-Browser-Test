"""A fixed, offline sample of what a finished comparison looks like, at ``pybrowser://demo/``.

This is NOT produced by a model. It is a hand-written page with fictional products so a new
person can see the shape of a result - their requirements checked one by one, a recommendation
labelled as judgement, figures tied to sources, conflicts and unreadable pages left visible -
before setting up a provider. The page says so at the top and nothing on it is presented as live
output. It has no scripts and reads nothing from the browser.
"""

from __future__ import annotations

from app.browser.internal import route

HOST = "demo"
DEMO_URL = f"pybrowser://{HOST}/"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sample result (demonstration)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --bg:#fafafc; --surface:#fff; --line:#d9d9e3; --text:#1a1a22; --muted:#555563;
          --accent:#2f49d9; --warn-bg:#fff3d6; --warn-text:#5c3d00; --ok:#146c2e; --bad:#a32020; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14141a; --surface:#1d1d26; --line:#3a3a48; --text:#ececf3;
          --muted:#b0b0bf; --accent:#9db0ff; --warn-bg:#3b2f10; --warn-text:#ffdf9a; --ok:#6fd48d; --bad:#ff9b9b; } }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.55 system-ui, "Segoe UI", sans-serif; }
  main { max-width: 820px; margin: 0 auto; padding: 24px 20px 64px; }
  .banner { background:var(--warn-bg); color:var(--warn-text); border-radius:10px; padding:12px 16px; margin-bottom:20px; }
  h1 { font-size:1.6rem; margin:.2em 0 .1em; } h2 { font-size:1.15rem; margin:1.6em 0 .4em; }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:14px 18px; }
  table { border-collapse:collapse; width:100%; } th,td { text-align:left; border-bottom:1px solid var(--line); padding:8px 10px; vertical-align:top; }
  .tag { font-size:.85rem; color:var(--muted); } .met { color:var(--ok); font-weight:600; } .notmet { color:var(--bad); font-weight:600; }
  sup { font-size:.75em; } code { font-size:.9em; } ul { padding-left: 1.2em; }
</style>
</head>
<body>
<main>
  <div class="banner" role="note"><strong>Demonstration only.</strong> This page was written by hand with
  fictional products. No AI model was used and nothing here comes from a real website. It shows the layout of a
  real result so you can see what to expect; set up a provider (Tools &rarr; AI &rarr; Configure AI Agent) to get your own.</div>

  <p class="tag">Sample mission</p>
  <h1>Which desk lamp should I buy?</h1>
  <p class="tag">Compared: Lumo L1, Brightly B2, Glowtek G3 &middot; run on a sample date</p>

  <h2>Recommendation <span class="tag">(the team&rsquo;s judgement, not a sourced fact)</span></h2>
  <div class="card"><p><strong>Lumo L1</strong> fits both of your requirements on the pages that could be read. Brightly B2
  is cheaper but its page did not say whether it ships to Canada.</p></div>

  <h2>Your requirements</h2>
  <div class="card"><table>
    <tr><th>Requirement (your words)</th><th>Lumo L1</th><th>Brightly B2</th><th>Glowtek G3</th></tr>
    <tr><td>Under $60</td><td class="met">Met &mdash; $54 <sup>[S1]</sup></td><td class="met">Met &mdash; $39 <sup>[S2]</sup></td><td class="tag">Not checked: page unreadable</td></tr>
    <tr><td>Ships to Canada</td><td class="met">Met <sup>[S1]</sup></td><td class="notmet">Not found in the sources</td><td class="tag">Not checked: page unreadable</td></tr>
  </table></div>

  <h2>Conflicts and uncertainties</h2>
  <div class="card"><ul>
    <li>Price of Lumo L1: <strong>$54</strong> on the maker&rsquo;s page <sup>[S1]</sup>, <strong>$59</strong> at a reseller <sup>[S3]</sup>. Both are shown; the team did not pick one.</li>
    <li>Brightly B2&rsquo;s shipping destinations are not stated on the page that was read <sup>[S2]</sup>.</li>
    <li>Glowtek G3&rsquo;s page asked for a sign-in, so nothing from it is used. PyBrowser does not bypass sign-in pages.</li>
  </ul></div>

  <h2>Sources and how they were read</h2>
  <div class="card"><table>
    <tr><th></th><th>Source</th><th>What the team had</th><th>Retrieved</th></tr>
    <tr><td>S1</td><td>lumo.example/l1</td><td>Page text (opened)</td><td>sample date</td></tr>
    <tr><td>S2</td><td>brightly.example/b2</td><td>Page text (opened)</td><td>sample date</td></tr>
    <tr><td>S3</td><td>shop.example/lumo-l1</td><td>Search snippet only &mdash; the page was not opened</td><td>sample date</td></tr>
    <tr><td>S4</td><td>glowtek.example/g3</td><td class="notmet">Could not be read (sign-in required)</td><td>&mdash;</td></tr>
  </table></div>

  <h2>Automatic citation check</h2>
  <div class="card"><p>A wording-and-figures check compares each cited sentence with the saved text of its source.
  It found the figures $54 and $39 in S1 and S2. It is <strong>not a fact check</strong>: it cannot tell whether a
  source is right, and a faithful paraphrase can fail it. Read the sources yourself for anything that matters.</p></div>

  <h2>Coming back later</h2>
  <p>Missions are saved on this computer. Close the browser and reopen the Mission to continue: finished work is kept,
  interrupted work resumes, and nothing that changes something outside your browser is repeated without asking.</p>
</main>
</body>
</html>
"""

route(HOST, lambda _url: _PAGE)
