(() => {
  "use strict";

  document.getElementById("year").textContent = new Date().getFullYear();

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

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
    if (changed && state === "finished") playFinishPop(anchor);
  }

  document.querySelectorAll("[data-story]").forEach((story) => {
    const anchor = story.querySelector("[data-mascot-anchor]");
    const steps = Array.from(story.querySelectorAll(".story-step"));
    if (!anchor || !steps.length) return;

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
          setMascot(anchor, entry.target.dataset.state, entry.target.dataset.caption);
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

  // -- Meet Py: hover/click/keyboard preview, plus a slow auto-demo --------------------------------------------------------
  const showcase = document.querySelector("[data-state-showcase]");
  if (showcase) {
    const cards = Array.from(showcase.querySelectorAll(".state-card"));
    let autoTimer = null;

    function stopAuto() {
      if (autoTimer) { window.clearInterval(autoTimer); autoTimer = null; }
      cards.forEach((c) => c.removeAttribute("data-spotlight"));
    }
    function play(card) {
      card.classList.add("is-playing");
    }
    function stop(card) {
      card.classList.remove("is-playing");
    }

    cards.forEach((card) => {
      card.addEventListener("mouseenter", () => play(card));
      card.addEventListener("mouseleave", () => stop(card));
      card.addEventListener("focus", () => play(card));
      card.addEventListener("blur", () => stop(card));
      card.addEventListener("click", () => {
        stopAuto();
        play(card);
        window.setTimeout(() => stop(card), 1800);
      });
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          card.click();
        }
      });
    });

    // A slow, quiet spotlight moves from card to card so the showcase reads
    // as alive even before anyone touches it - never faster than a person
    // could comfortably read the caption underneath, and it stops for good
    // the moment someone interacts with the showcase themselves.
    if (!reducedMotion.matches && cards.length) {
      let i = 0;
      cards[0].setAttribute("data-spotlight", "");
      autoTimer = window.setInterval(() => {
        cards[i].removeAttribute("data-spotlight");
        stop(cards[i]);
        i = (i + 1) % cards.length;
        cards[i].setAttribute("data-spotlight", "");
        play(cards[i]);
        window.setTimeout(() => stop(cards[i]), 2600);
      }, 4200);
      showcase.addEventListener("pointerdown", stopAuto, { once: true });
      showcase.addEventListener("focusin", stopAuto, { once: true });
    }
  }

  // -- hero mascot: a one-time settle animation, then idle --------------------------------------------------------
  const heroAnchor = document.querySelector(".hero-mascot[data-mascot-anchor]");
  if (heroAnchor && !reducedMotion.matches) {
    heroAnchor.style.transform = "translateY(8px)";
    heroAnchor.style.opacity = "0";
    requestAnimationFrame(() => {
      heroAnchor.style.transition = "transform .5s ease, opacity .5s ease";
      heroAnchor.style.transform = "translateY(0)";
      heroAnchor.style.opacity = "1";
    });
  }

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
