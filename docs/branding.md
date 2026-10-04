# Branding

The logo appears on every page: sidebar, sign-in page and mobile header. It is served from

```
frontend/public/brand/logo.svg
```

1. Download the **official** logo (SVG preferred; light/white version for the dark sidebar and sign-in
   page) from your brand portal, and follow its usage guidelines (clear space, minimum size).
2. Save it as `frontend/public/brand/logo.svg`.
3. Rebuild the image (`docker compose up --build` or the pipeline).

Until the file exists, a plain text wordmark is shown. The repo deliberately does not ship a
hand-drawn copy of a company logo.

Brand colours are CSS variables at the top of [`frontend/src/styles.css`](../frontend/src/styles.css)
(`--brand`, `--brand-strong`, `--brand-soft`, `--nav-bg`). They are approximations: replace them with
the official values. Chart colours are separate on purpose (validated for colour-blind safety and
contrast) and should not be swapped for brand colours.
