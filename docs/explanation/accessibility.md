# Accessibility

The target is **WCAG 2.2 level AA**, the level every accessibility law below
points at. A measurement backs the claim; nobody assumed it. This page
records the measurement, the changes, the guards, and the known gaps.

## Contrast, measured

The measurement of 2026-08-24 ran WCAG's own relative-luminance formula over
the neo-greco theme (`ui/static/theme.css`, brand colours in `main.py`). The
thresholds are **4.5:1** for text and **3:1** for large text (24px, or
18.66px bold). The 3:1 threshold also covers the parts of a control that a
person has to see. Under 1.4.11 those are a field's border, a focus ring, a
graph edge.

These pairs passed unchanged: body ink on parchment (11.7:1), headings and
table captions (4.8:1), links (6.5:1). So did white on the terracotta header
(5.2:1), the focus ring (4.8:1), tooltips (8.4:1) and dark-mode body text
(13.3:1).

What failed, and the fix:

| Where | Was | Now |
|---|---|---|
| `text-gray-500` — the app's secondary text, ~90 sites | 4.48:1 | remapped to `#5f574c` (6.6:1); `text-gray-400` (2.35:1) to `#6b6255` (5.6:1). The same trick the dark block already used. |
| Grey badges ("inactive", "Cancelled", "no account") | 2.7:1 white on `#9e9e9e` | a `muted` brand colour, `#6b6255` (6.0:1) |
| `warning` badges and text | 3.6:1 / 3.4:1 | `#8f6218` (5.4:1 / 5.0:1) |
| `positive` as text | 4.47:1 | `#527037` (5.2:1) |
| `secondary` badges | 4.4:1 | `#766240` (5.9:1) |
| Header email at 80 % opacity | ~3.7:1 | full opacity |
| Outlined field borders (Quasar's `rgba(0,0,0,.24)`) | 1.9:1 | `#8a8072` (3.6:1) |
| Workload band badges, admin-picked colours | 2.8 / 1.8 / 4.2:1 with white text | the label colour is computed per band (ink or white, whichever reads); the red default is `#c62828`; the admin page shows the ratio and refuses a band under 4.5:1 |
| Graph edges | 1.5:1 | `#8a8072` (3.6:1) |
| **Dark mode**: every filled button and badge | 2.2–4.3:1 white on the tint | dark-mode fills carry **ink labels** (`#1c1917`, 5.3–8:1); `negative` re-tinted to `#d9755f` so it also reads as text (5.6:1) |

`tests/test_theme_contrast.py` reads the tokens and brand colours out of the
source and asserts every pair above. So a re-tint that drops one under the
line fails the suite. `tests/e2e/test_browser_a11y.py` runs axe-core over
the main pages in both modes and fails on any *serious* or *critical*
finding.

## The rest of AA that cost little

- **3.1.1** `<html lang="en">` on every page (`ui.run(language="en")`).
- **4.1.2** every icon-only control has an accessible name
  (`ui/a11y.py: icon_button`). That covers the header gear, sign-out and
  menu, the account-row actions, and the date picker's calendar button.
- **2.1.1** the keyboard can reach clickable table rows. The events and
  teams tables carry a real link in the title cell, the volunteers table a
  button in the name cell. The row click stays for the mouse.
- **2.4.1 / 1.3.1** a skip link ("Skip to content", visible on focus) into
  the page body, and a `<nav aria-label="Main">`. The `<main>` landmark is
  NiceGUI's own page container.
- **1.4.1** links in body text carry an underline; before, colour was the
  only difference (link vs. ink is 1.6:1).
- **2.4.7** the focus ring is two-tone (a parchment halo inside a terracotta
  ring), so it shows on a terracotta button too.
- **2.3.3** `prefers-reduced-motion` switches transitions off.
- **2.5.8** the date picker's trigger is a real 32px button, not a 20px icon
  with a click handler.
- **3.3.8** the sign-in already qualifies: no cognitive test, password
  managers work (`autocomplete` on every field), an emailed code as the
  fallback.

## Native HTML over script

Where the browser now does the job, it does it here. The month calendar on
`/events` is a `<table>` with `<time>` cells, and a link changes the month
or the view. The subscribe panel is a native `popover`, and a feed-address
rotation is a `<form>`. Downloads are `<a download>` on plain GET routes. A
filled slot count is a `<meter>`. What deliberately stays a Quasar
component, and why:

- **Dialogs.** Quasar menus (`ui.select`, `ui.date`, `ui.menu`) portal to
  `<body>`, which sits *below* the browser's top layer. So a date picker
  inside a native `<dialog>` renders hidden. Only dialogs with no menu in
  them could move, and the gain would be inconsistency.
- **`<datalist>`** for the search selects: its screen-reader behaviour is
  worse than QSelect's (see Adrian Roselli, *Under-Engineered Comboboxen*).
- **`title=`** for tooltips: neither the keyboard nor touch can reach it;
  the fix is an `aria-label`, not a swap.
- **`<input type=date>`**: the Quasar picker has one keyboard model, a mask
  and dark mode; native date inputs vary per browser and screen reader.
- **`command`/`commandfor`**: only the popover/dialog commands are stable;
  `popovertarget` covers the same ground with wider support.

## Known gaps

- Drag-to-reorder for table columns has no keyboard path. It is a
  preference, not a function: every column stays visible and sortable
  without it.
- Quasar's `ui.menu_item`s announce as list items, not links, in the
  narrow-screen menu.
- Ministry home pages are Google Docs that the parish writes; the contrast
  inside them is whatever the document has.
- The graph's grey nodes sit at 2.5:1 against the page. Their labels read at
  4.7:1, and the same relationships are on every team page as text.

## Other standards, for orientation

- **WCAG 2.2** (W3C Recommendation, October 2023) — the base every law below
  cites. New in 2.2 and relevant here: focus not obscured (2.4.11), target
  size (2.5.8), redundant entry (3.3.7), accessible authentication (3.3.8).
- **AODA, O. Reg. 191/11 s. 14** (Ontario) — WCAG 2.0 AA for designated
  public-sector bodies. It also binds private and non-profit organisations
  with 50 or more employees. A parish is likely under the threshold; it is
  still the provincial yardstick.
- **Accessible Canada Act** (2019) and the CAN/ASC standards under it.
- **EN 301 549** (the EU procurement standard, WCAG 2.1 AA) and the
  **European Accessibility Act**, in force since June 2025.
- **US Section 508** — WCAG 2.0 AA since the 2017 refresh. The **ADA Title
  II rule** (2024) sets WCAG 2.1 AA for state and local government sites
  from 2026–27.
- **ISO/IEC 40500** — WCAG 2.0 as an ISO standard.
- **WAI-ARIA 1.2** and the **ARIA Authoring Practices Guide** — the widget
  patterns (menus, dialogs, grids) that the Quasar components are held to.
- **ATAG 2.0** — authoring-tool guidelines; marginal here, since leaders
  author public pages in Google Docs.
