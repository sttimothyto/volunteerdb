# Set the site logo

The parish's own mark replaces the shipped placeholder in 3 places:

- the app header (beside *Dash*)
- above the login box
- the shell of the public [ministry pages](team-home-pages.md)

There is one logo per instance. A new upload replaces the old one.

## Upload it (admin)

1. Sign in as an administrator.
2. Click the logo at the left end of the header. The *Site logo* dialog
   opens. The logo is the only way in; there is no settings page for it.
3. Drop an image on the upload box, or use its picker. Any common image
   format works, up to 10 MB.
4. Check the preview. It redraws at once, on the header's own terracotta,
   the ground the logo will sit on. So a white box shows up here, not on
   every page.
5. Click *Upload*. Every page picks the logo up on its next load, the
   signed-out pages included.

*Remove logo* in the same dialog goes back to the placeholder.

## What happens to the image

- The site scales the image to fit 1000 × 1000 pixels. It never crops and
  never enlarges, so a wide wordmark keeps its shape.
- The site keeps transparency.
- An image with no transparency whose border is a flat card (a JPEG, or a
  PNG exported from one) loses that card. The mark stays on a transparent
  ground. White *inside* the mark, such as the lettering on a shield or the
  counters of a monogram, stays.
- The site stores the image as a PNG under 500 KB. If a photographed or
  scanned mark does not fit, the site first reduces it to 256 colours. If it
  still does not fit, the site refuses it with a message.

## Operations

- The site serves the logo at `/logo` to anyone, signed in or not: the
  login page and the public shell need it.
- The response is `Cache-Control: no-cache` with an ETag. Browsers
  revalidate on every page, and a new upload shows at once, at the cost of
  one small request per page.
- The {ref}`site_logo table <site_logo>` holds the logo: one row, not
  versioned. The nightly backup carries it like everything else.
- There is no API endpoint for the logo. The dialog is the only way to
  change it.
