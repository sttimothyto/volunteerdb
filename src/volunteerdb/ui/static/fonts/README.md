# Fonts served by the site

Both faces are self-hosted so the site reads the same on every device, and
subset to Latin (the range Google Fonts calls `latin`) so each is a few tens
of kilobytes.

- `cinzel-v11-latin-regular.woff2` — Cinzel, the display face (headings, the
  header). SIL Open Font License 1.1. From Google Fonts.
- `texgyrepagella-*-latin.woff2` — TeX Gyre Pagella, the body serif (a
  Palatino-alike; the `--vdb-serif` stack falls back to Palatino and Georgia).
  GUST Font License (GFL), a free licence permitting redistribution and
  embedding. From <https://www.gust.org.pl/projects/e-foundry/tex-gyre/pagella>,
  version 2.501, subset with `pyftsubset --flavor=woff2 --layout-features='*'`
  over Google's latin unicode range.

`theme.css` declares the faces; `main.py` preloads the two regular files.
