// DEV-ONLY. Reads ?art=a|b|c (or a saved choice) and stamps
// data-art on <html> so art-prototypes.css can take over the
// background layer. Does nothing at all for a normal visitor with no
// query param and no saved preview choice - the shipped site is
// unaffected. Delete this file + art-prototypes.css + the <link>/
// <script> tags in index.html once a direction is chosen.
(() => {
  "use strict";
  const params = new URLSearchParams(window.location.search);
  const KEY = "pybrowser-art-preview";
  let art = params.get("art");

  if (art) {
    if (art === "off") {
      localStorage.removeItem(KEY);
      art = null;
    } else {
      try { localStorage.setItem(KEY, art); } catch (e) { /* ignore */ }
    }
  } else {
    try { art = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
  }

  if (art && ["a", "b", "c"].includes(art)) {
    document.documentElement.setAttribute("data-art", art);
  }

  // A tiny floating switcher, only ever rendered when explicitly asked
  // for via ?artdev=1 - never shown to a normal visitor or reviewer
  // just clicking around with ?art=a in the URL.
  if (params.get("artdev") === "1") {
    window.addEventListener("DOMContentLoaded", () => {
      const bar = document.createElement("div");
      bar.style.cssText =
        "position:fixed;bottom:16px;left:16px;z-index:99999;display:flex;" +
        "gap:6px;font:600 12px system-ui;background:#111;padding:8px;border-radius:10px;" +
        "box-shadow:0 8px 24px rgba(0,0,0,.4);";
      ["off", "a", "b", "c"].forEach((v) => {
        const btn = document.createElement("button");
        btn.textContent = v.toUpperCase();
        btn.style.cssText =
          "padding:6px 10px;border-radius:6px;border:1px solid #444;cursor:pointer;" +
          "background:" + (art === v || (v === "off" && !art) ? "#fff" : "#222") + ";" +
          "color:" + (art === v || (v === "off" && !art) ? "#111" : "#fff") + ";";
        btn.addEventListener("click", () => {
          const url = new URL(window.location.href);
          url.searchParams.set("art", v);
          window.location.href = url.toString();
        });
        bar.appendChild(btn);
      });
      document.body.appendChild(bar);
    });
  }
})();
