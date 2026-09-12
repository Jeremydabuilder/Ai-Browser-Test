(() => {
  "use strict";

  document.getElementById("year").textContent = new Date().getFullYear();

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // Gates every CSS rule that hides content until JS reveals it (scroll
  // reveals, the story's dimmed-until-active steps) - added only once this
  // script has actually run, so a no-JS visitor, or one whose script failed
  // to load, always sees the fully-visible, un-animated page rather than
  // content stuck at opacity:0 waiting for a callback that will never fire.
  document.documentElement.classList.add("js-ready");

  // -- generic scroll reveal --------------------------------------------------------
  // Fades/rises any [data-reveal] element into place the first time it
  // enters the viewport, then stops watching it - a one-time entrance, not
  // a toggle that replays every time someone scrolls past. Cheap (one
  // shared observer, transform/opacity only) and skipped instantly under
  // reduced motion by the blanket transition-duration override in CSS.
  const revealTargets = document.querySelectorAll("[data-reveal]");
  if (revealTargets.length) {
    if (reducedMotion.matches || !("IntersectionObserver" in window)) {
      revealTargets.forEach((el) => el.classList.add("is-visible"));
    } else {
      const revealObserver = new IntersectionObserver(
        (entries, obs) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            entry.target.classList.add("is-visible");
            obs.unobserve(entry.target);
          });
        },
        { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
      );
      revealTargets.forEach((el) => revealObserver.observe(el));
    }
  }

  // -- mobile nav --------------------------------------------------------
  const burger = document.getElementById("nav-burger");
  const mobileNav = document.getElementById("mobile-nav");
  if (burger && mobileNav) {
    burger.addEventListener("click", () => {
      const open = mobileNav.hasAttribute("data-open");
      if (open) {
        mobileNav.removeAttribute("data-open");
        mobileNav.hidden = true;
        burger.setAttribute("aria-expanded", "false");
      } else {
        mobileNav.hidden = false;
        mobileNav.setAttribute("data-open", "");
        burger.setAttribute("aria-expanded", "true");
      }
    });
    mobileNav.querySelectorAll("a").forEach((a) => {
      a.addEventListener("click", () => {
        mobileNav.removeAttribute("data-open");
        mobileNav.hidden = true;
        burger.setAttribute("aria-expanded", "false");
      });
    });
  }

  // -- theme toggle --------------------------------------------------------
  // Defaults to the system preference; a manual choice is remembered
  // per-visitor only (localStorage), the same "follow the OS, let a
  // person override it" model the desktop app itself uses.
  const root = document.documentElement;
  const themeToggle = document.getElementById("theme-toggle");
  const STORAGE_KEY = "pybrowser-site-theme";

  function applyStoredTheme() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "light" || stored === "dark") {
        root.setAttribute("data-theme", stored);
      }
    } catch (e) { /* storage unavailable - fall back to system preference */ }
  }
  applyStoredTheme();

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const current = root.getAttribute("data-theme") ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(STORAGE_KEY, next); } catch (e) { /* ignore */ }
    });
  }

  // -- scroll-guided Py narration --------------------------------------------------------
  // Each `[data-story]` section holds a sticky Py (`[data-mascot-anchor]`)
  // and a column of `.story-step` panels. As a step crosses the middle of
  // the viewport, Py's image and caption swap to match it. Respects
  // prefers-reduced-motion by skipping the crossfade and swapping instantly.
  // Every Py host (the scroll-story mascot and each Meet-Py preview card)
  // gets the same small decoration spans the CSS keyframes target - three
  // rising dots for "thinking", two blinking taps for "working" - injected
  // once here rather than hand-duplicated in the markup for each instance.
  function injectDecorations(host) {
    if (host.querySelector(".think-bubbles")) return;
    const bubbles = document.createElement("span");
    bubbles.className = "think-bubbles";
    bubbles.setAttribute("aria-hidden", "true");
    bubbles.innerHTML = "<span></span><span></span><span></span>";
    host.appendChild(bubbles);
    const taps = document.createElement("span");
    taps.className = "type-taps";
    taps.setAttribute("aria-hidden", "true");
    taps.innerHTML = "<span></span><span></span>";
    host.appendChild(taps);
    // The same ring motif used decoratively elsewhere on the page (see
    // .motif-rings), reused here as a literal "processing" indicator - a
    // partial ring that only appears and spins while Py is actively doing
    // something (searching/reading/thinking/working), so the brand's own
    // geometry becomes the busy-state cue instead of a generic spinner.
    const orbit = document.createElement("span");
    orbit.className = "py-orbit";
    orbit.setAttribute("aria-hidden", "true");
    host.appendChild(orbit);
  }
  document.querySelectorAll(".story-py, .py-stage-mascot").forEach(injectDecorations);

  const MASCOT_SRC = {
    idle: "assets/mascot/py-idle.webp",
    searching: "assets/mascot/py-searching.webp",
    reading: "assets/mascot/py-reading.webp",
    thinking: "assets/mascot/py-thinking.webp",
    working: "assets/mascot/py-working.webp",
    approval: "assets/mascot/py-approval.webp",
    finished: "assets/mascot/py-complete.webp",
    stuck: "assets/mascot/py-stuck.webp",
  };

  // A short "anticipation" beat before the crossfade - a small dip in scale
  // and opacity, like a breath held for a beat - rather than a hard cut from
  // one pose straight to the next. Runs on the compositor only (transform +
  // opacity), and is skipped entirely under reduced motion.
  function crossfadeTo(img, src) {
    if (reducedMotion.matches || !img.animate) {
      img.src = src;
      return;
    }
    const out = img.animate(
      [{ transform: "scale(1)", opacity: 1 }, { transform: "scale(.93)", opacity: .35 }],
      { duration: 180, easing: "ease-in", fill: "forwards" }
    );
    out.onfinish = () => {
      img.src = src;
      img.animate(
        [{ transform: "scale(.93)", opacity: .35 }, { transform: "scale(1.03)", opacity: 1 },
         { transform: "scale(1)", opacity: 1 }],
        { duration: 260, easing: "cubic-bezier(.2,.7,.3,1.2)" }
      );
    };
  }

  // A one-time success beat on reaching "finished" - not a loop, just a
  // single small pop, so a Mission actually completing feels like a small
  // event rather than another idle animation running in the background.
  function playFinishPop(anchor) {
    const img = anchor.querySelector("[data-mascot-img]");
    if (!img || reducedMotion.matches || !img.animate) return;
    img.animate(
      [{ transform: "scale(1) rotate(0deg)" },
       { transform: "scale(1.12) rotate(-3deg)", offset: .5 },
       { transform: "scale(1) rotate(0deg)" }],
      { duration: 520, easing: "cubic-bezier(.2,.8,.3,1.2)" }
    );
  }

  // A one-time confetti burst for the same "finished" beat - small pieces in
  // the site's own accent colors, launched outward and up like they've been
  // thrown, then falling past their start point under gravity while still
  // tumbling, rather than just fading out where they landed. Each piece's
  // rise is eased out (decelerating, like something losing its throw speed)
  // and its fall is eased in (accelerating, like something actually
  // dropping), which is what makes it read as gravity rather than a generic
  // float. Cleaned up from the DOM as each piece's own animation ends.
  function spawnConfetti(container, origin) {
    if (!container || reducedMotion.matches) return;
    const probe = document.createElement("span");
    if (typeof probe.animate !== "function") return;
    const originEl = origin || container;
    const cRect = container.getBoundingClientRect();
    const oRect = originEl.getBoundingClientRect();
    const originX = oRect.left + oRect.width / 2 - cRect.left;
    const originY = oRect.top + oRect.height / 2 - cRect.top;
    const colors = [
      "var(--spectrum-1)", "var(--spectrum-2)", "var(--spectrum-3)",
      "var(--spectrum-4)", "var(--spectrum-5)",
    ];
    const rise = "cubic-bezier(.16,.85,.35,1)"; // decelerating, like losing throw speed
    const fall = "cubic-bezier(.55,0,.85,.45)"; // accelerating, like gravity taking over
    for (let i = 0; i < 16; i++) {
      const piece = document.createElement("span");
      piece.className = "confetti-piece";
      piece.style.left = `${originX}px`;
      piece.style.top = `${originY}px`;
      piece.style.background = colors[i % colors.length];
      piece.style.borderRadius = i % 2 === 0 ? "50%" : "2px";
      container.appendChild(piece);
      const angle = (Math.random() * 130 - 65) * (Math.PI / 180);
      const throwUp = 46 + Math.random() * 46;
      const drift = (Math.sin(angle) * throwUp) + (Math.random() * 30 - 15);
      const spin = (Math.random() < 0.5 ? -1 : 1) * (280 + Math.random() * 360);
      const duration = 950 + Math.random() * 550;
      const wobble = Math.random() * 18 - 9; // a slight mid-air drift correction, like air resistance
      const anim = piece.animate(
        [
          { transform: "translate(-50%, -50%) rotate(0deg) scale(.5)", opacity: 1, offset: 0 },
          {
            transform: `translate(calc(-50% + ${(drift * 0.35).toFixed(1)}px), calc(-50% + ${(-throwUp).toFixed(1)}px)) rotate(${(spin * 0.3).toFixed(0)}deg) scale(1)`,
            opacity: 1,
            offset: 0.22,
            easing: rise,
          },
          {
            transform: `translate(calc(-50% + ${(drift * 0.6 + wobble).toFixed(1)}px), calc(-50% + ${(-throwUp * 0.55).toFixed(1)}px)) rotate(${(spin * 0.6).toFixed(0)}deg) scale(.95)`,
            opacity: 1,
            offset: 0.45,
            easing: fall,
          },
          {
            transform: `translate(calc(-50% + ${(drift * 0.85).toFixed(1)}px), calc(-50% + ${(throwUp * 0.7).toFixed(1)}px)) rotate(${(spin * 0.85).toFixed(0)}deg) scale(.85)`,
            opacity: 1,
            offset: 0.75,
            easing: fall,
          },
          {
            transform: `translate(calc(-50% + ${drift.toFixed(1)}px), calc(-50% + ${(throwUp * 1.7).toFixed(1)}px)) rotate(${spin.toFixed(0)}deg) scale(.7)`,
            opacity: 0,
          },
        ],
        { duration, fill: "forwards" }
      );
      anim.onfinish = () => piece.remove();
    }
  }

  function setMascot(anchor, state, caption) {
    const img = anchor.querySelector("[data-mascot-img]");
    const cap = anchor.querySelector("[data-py-caption]");
    const stateLabel = anchor.querySelector("[data-py-state]");
    const src = MASCOT_SRC[state];
    const changed = anchor.dataset.state !== state;
    if (img && src && !img.src.endsWith(src)) {
      crossfadeTo(img, src);
    }
    if (changed) anchor.dataset.state = state;
    if (cap && caption && cap.textContent !== caption) {
      if (reducedMotion.matches) {
        cap.textContent = caption;
      } else {
        cap.style.opacity = "0";
        window.setTimeout(() => {
          cap.textContent = caption;
          cap.style.opacity = "1";
        }, 150);
      }
    }
    if (stateLabel) stateLabel.textContent = state.charAt(0).toUpperCase() + state.slice(1);
    if (changed && state === "finished") {
      playFinishPop(anchor);
      spawnConfetti(anchor, img);
    }
  }

  document.querySelectorAll("[data-story]").forEach((story) => {
    const anchor = story.querySelector("[data-mascot-anchor]");
    const steps = Array.from(story.querySelectorAll(".story-step"));
    if (!anchor || !steps.length) return;

    // The one screenshot this story is building toward - if it has one -
    // gets a single glow pulse the moment "Finished" actually becomes the
    // active step, so reaching the end of the demo reads as a small event.
    const finishShot = story.querySelector("[data-finish-shot]");
    function markFinished(state) {
      if (state === "finished" && finishShot && !reducedMotion.matches) {
        finishShot.classList.add("just-finished");
      }
    }

    const initial = steps[0];
    anchor.dataset.state = initial.dataset.state;
    setMascot(anchor, initial.dataset.state, initial.dataset.caption);
    initial.setAttribute("data-active", "");

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          steps.forEach((s) => s.removeAttribute("data-active"));
          entry.target.setAttribute("data-active", "");
          const changedToState = anchor.dataset.state !== entry.target.dataset.state;
          setMascot(anchor, entry.target.dataset.state, entry.target.dataset.caption);
          if (changedToState) markFinished(entry.target.dataset.state);
        });
      },
      { rootMargin: "-45% 0px -45% 0px", threshold: 0 }
    );
    steps.forEach((step) => observer.observe(step));
  });

  // -- hero: look toward whatever the visitor is considering --------------------------------------------------------
  const heroMascotEl = document.querySelector(".hero-mascot[data-mascot-anchor]");
  const heroCta = document.querySelector(".hero-ctas .btn-primary");
  const heroShot = document.querySelector(".shot-frame--hero");
  if (heroMascotEl && !reducedMotion.matches) {
    const look = (target, on) => {
      if (on) heroMascotEl.setAttribute("data-look", target);
      else if (heroMascotEl.getAttribute("data-look") === target) heroMascotEl.removeAttribute("data-look");
    };
    if (heroCta) {
      heroCta.addEventListener("mouseenter", () => look("cta", true));
      heroCta.addEventListener("mouseleave", () => look("cta", false));
      heroCta.addEventListener("focus", () => look("cta", true));
      heroCta.addEventListener("blur", () => look("cta", false));
    }
    if (heroShot) {
      heroShot.addEventListener("mouseenter", () => look("product", true));
      heroShot.addEventListener("mouseleave", () => look("product", false));
    }
  }

  // -- hero: a small mouse-parallax on the whole visual cluster --------------------------------------------------------
  // Screenshot and Py move together as one plane against the static
  // background motif, capped to a few pixels - "this scene has depth,"
  // not a scroll-jacking parallax effect. Desktop-with-a-mouse only
  // (pointer:fine), rAF-throttled to at most one style write per frame.
  const heroSection = document.querySelector(".hero");
  const heroVisual = document.querySelector(".hero-visual");
  if (heroSection && heroVisual && !reducedMotion.matches &&
      window.matchMedia("(pointer: fine)").matches) {
    let raf = null;
    let px = 0, py = 0;
    heroSection.addEventListener("mousemove", (e) => {
      const rect = heroSection.getBoundingClientRect();
      const nx = (e.clientX - rect.left) / rect.width - 0.5;
      const ny = (e.clientY - rect.top) / rect.height - 0.5;
      px = nx * 10;
      py = ny * 8;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        heroVisual.style.setProperty("--par-x", `${px.toFixed(1)}px`);
        heroVisual.style.setProperty("--par-y", `${py.toFixed(1)}px`);
        raf = null;
      });
    });
    heroSection.addEventListener("mouseleave", () => {
      heroVisual.style.setProperty("--par-x", "0px");
      heroVisual.style.setProperty("--par-y", "0px");
    });
  }

  // -- Meet Py: one stage, a row of tabs, plus a slow auto-demo --------------------------------------------------------
  // A single large Py display driven by whichever tab is selected, reusing
  // setMascot (the same crossfade/caption/finish-pop/confetti logic every
  // other Py host uses) rather than six separate always-visible portraits.
  const showcase = document.querySelector("[data-state-showcase]");
  if (showcase) {
    const stageAnchor = showcase.querySelector(".py-stage-display");
    const nameEl = showcase.querySelector("[data-stage-name]");
    const tabs = Array.from(showcase.querySelectorAll(".py-tab"));
    let autoTimer = null;

    function stopAuto() {
      if (autoTimer) { window.clearInterval(autoTimer); autoTimer = null; }
    }
    function select(tab) {
      tabs.forEach((t) => t.setAttribute("aria-selected", t === tab ? "true" : "false"));
      if (nameEl) nameEl.textContent = tab.textContent;
      setMascot(stageAnchor, tab.dataset.state, tab.dataset.desc);
    }

    if (stageAnchor && tabs.length) {
      stageAnchor.dataset.state = tabs[0].dataset.state;
      tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
          stopAuto();
          select(tab);
        });
      });

      // A slow, quiet auto-advance through the states so the stage reads as
      // alive before anyone touches it - never faster than a person could
      // comfortably read the caption, and it stops for good the moment
      // someone picks a state themselves.
      if (!reducedMotion.matches) {
        let i = 0;
        autoTimer = window.setInterval(() => {
          i = (i + 1) % tabs.length;
          select(tabs[i]);
        }, 3400);
        showcase.addEventListener("pointerdown", stopAuto, { once: true });
        showcase.addEventListener("focusin", stopAuto, { once: true });
      }
    }
  }

  // -- hero mascot: arrives dim, then wakes once the browser switches on --------------------------------------------------------
  // Two separate beats, not one: Py first arrives into the scene (a beat
  // after the headline, per hero-rise's own delays) but stays visibly dim
  // - not yet "on". The actual wake (brightness ramp + the ring pulse in
  // CSS) is a second, later beat timed to land with the hero screenshot's
  // own assemble/sweep (see .shot-frame--hero in CSS), so the browser
  // visibly switches on first and Py's reaction reads as a response to it,
  // not two unrelated entrances racing each other.
  const heroAnchor = document.querySelector(".hero-mascot[data-mascot-anchor]");
  if (heroAnchor && !reducedMotion.matches) {
    heroAnchor.style.transform = "translateY(10px) scale(.96)";
    heroAnchor.style.opacity = "0";
    heroAnchor.style.filter = "brightness(.4) saturate(.55)";
    window.setTimeout(() => {
      heroAnchor.style.transition = "transform 560ms cubic-bezier(.16,1,.3,1), opacity 560ms cubic-bezier(.16,1,.3,1)";
      heroAnchor.style.transform = "none";
      heroAnchor.style.opacity = "1";
    }, 320);
    window.setTimeout(() => {
      heroAnchor.style.transition = "filter 650ms cubic-bezier(.16,1,.3,1)";
      heroAnchor.style.filter = "brightness(1) saturate(1)";
    }, 1150);
  }

  // -- final CTA: Py is back at rest, not finishing another Mission --------------------------------------------------------
  // The finish/confetti beat belongs to a completed Mission (the Research
  // story already plays it) - here Py has simply returned to idle,
  // listening for whatever comes next, so the only cue is the quiet
  // "listening" ring defined in CSS (self-timed, no JS needed) plus the
  // ordinary [data-reveal] fade already on .final-cta-inner.

  // -- scroll-spy nav highlighting --------------------------------------------------------
  const navLinks = Array.from(document.querySelectorAll(".nav-links a"));
  const sections = navLinks
    .map((a) => document.querySelector(a.getAttribute("href")))
    .filter(Boolean);
  if (sections.length) {
    const spy = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          const link = navLinks.find((a) => a.getAttribute("href") === `#${entry.target.id}`);
          if (!link) return;
          if (entry.isIntersecting) {
            navLinks.forEach((a) => a.removeAttribute("aria-current"));
            link.setAttribute("aria-current", "true");
          }
        });
      },
      { rootMargin: "-40% 0px -50% 0px" }
    );
    sections.forEach((s) => spy.observe(s));
  }
})();
