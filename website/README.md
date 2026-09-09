# PyBrowser website

The public marketing site for PyBrowser. Plain HTML/CSS/JS — no build step,
no framework, no bundler.

```
website/
  index.html        the whole site (single page, anchored sections)
  css/style.css      design tokens + all styling, light/dark via CSS variables
  js/main.js          nav, theme toggle, scroll-guided Py narration
  assets/
    brand/            the π logo (SVG) and generated favicons
    mascot/            Py's states, exported from the app's real art as .webp
    screenshots/       real PyBrowser screenshots (see below)
```

## Run it locally

Any static file server works. From the `website/` directory:

```bash
python3 -m http.server 8000
# then open http://localhost:8000/
```

## Build for production

There is no build step — `website/` *is* the production artifact. Copy or
sync the directory as-is.

## Deploying

Any static host works: GitHub Pages, Netlify, Vercel, Cloudflare Pages, or a
plain S3/nginx bucket. Point it at `website/` and serve `index.html`. No
environment variables, no server-side code, no database.

## Regenerating the screenshots

`assets/screenshots/*.png` and `assets/mascot/*.webp` are real captures of
the actual PyBrowser application (`MainWindow`, a real `MissionService`, a
real `AgentSession` driven by the test suite's scripted model in
`tests/fake_claude.py`) — not mockups. If the product UI changes, regenerate
them by running the real app offscreen and calling `.grab().save(...)` on
the window; see `tests/test_e2e_missions.py` and `tests/fixture_server.py`
for the patterns used to drive a real Mission end to end.

## Editing the brand mark

`assets/brand/pi-mark.svg` is the standalone π mark (`currentColor`, no
background) used in the nav and footer. `assets/brand/pi-badge.svg` is the
same mark on a rounded gradient badge, used for the favicon and social
image. The PNG/ICO favicons under `assets/brand/` are rasterized from
`pi-badge.svg` — regenerate them from that file if the mark changes.
