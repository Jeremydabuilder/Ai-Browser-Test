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

  function setMascot(anchor, state, caption) {
    const img = anchor.querySelector("[data-mascot-img]");
    const cap = anchor.querySelector("[data-py-caption]");
    const stateLabel = anchor.querySelector("[data-py-state]");
    const src = MASCOT_SRC[state];
    if (img && src && !img.src.endsWith(src)) {
      if (reducedMotion.matches) {
        img.src = src;
      } else {
        img.style.opacity = "0";
        window.setTimeout(() => {
          img.src = src;
          img.style.opacity = "1";
        }, 120);
      }
    }
    if (cap && caption) cap.textContent = caption;
    if (stateLabel) stateLabel.textContent = state.charAt(0).toUpperCase() + state.slice(1);
  }

  document.querySelectorAll("[data-story]").forEach((story) => {
    const anchor = story.querySelector("[data-mascot-anchor]");
    const steps = Array.from(story.querySelectorAll(".story-step"));
    if (!anchor || !steps.length) return;

    const initial = steps[0];
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
