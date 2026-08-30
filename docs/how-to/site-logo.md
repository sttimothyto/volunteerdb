# Set the site logo

The parish's own mark replaces the shipped placeholder in three places: the
app header (beside *Dash*), above the login box, and on the shell of the
public [ministry pages](team-home-pages.md). One logo per instance —
uploading another replaces it.

## Upload it (admin)

1. Sign in as an administrator and click the logo at the left end of the
   header. The **Site logo** dialog opens (the logo is the only way in; there
   is no settings page for it).
2. Drop an image on the upload box, or use its picker. Any common image
   format, up to 10 MB. The preview redraws at once, on the header's own
   terracotta — the ground the logo will actually sit on — so a white box
   shows up here rather than on every page.
3. Click **Upload**. Every page picks it up on its next load, the signed-out
   ones included.

**Remove logo** in the same dialog goes back to the placeholder.

## What happens to the image

- Scaled to fit 1000 × 1000 pixels — never cropped, never enlarged — so a
  wide wordmark keeps its shape.
- Transparency is kept. An image with no transparency whose border is a flat
  card (a JPEG, or a PNG exported from one) has that card cut away, leaving
  the mark on a transparent ground; white *inside* the mark — lettering on a
  shield, the counters of a monogram — stays.
- Stored as PNG under 500 KB. A photographed or scanned mark that will not
  fit is reduced to 256 colours first, and refused with a message if it still
  will not.

## Operations

- Served at `/logo` to anyone, signed in or not: the login page and the
  public shell need it. The response is `Cache-Control: no-cache` with an
  ETag, so browsers revalidate on every page and a new upload shows
  immediately, at the cost of one small request per page.
- Stored in the {ref}`site_logo table <site_logo>` — one row, not
  versioned — so the nightly backup carries it like everything else.
- There is no API endpoint for it; the dialog is the only way to change it.
