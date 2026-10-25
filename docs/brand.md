# Shoal brand

A shoal is a group of fish swimming together. The Shoal mark is a school of small fish that together
form one larger fish, led by a single coral fish: many agents, one workflow, with a run out in front.

All files are in [`docs/assets/brand/`](assets/brand/). The wordmark is outlined (no font needed) and
set in Inter Bold.

## Logo

| File | Use |
|---|---|
| `shoal-logo.svg` | Mark and wordmark on light backgrounds |
| `shoal-logo-dark.svg` | Mark and wordmark on dark backgrounds |
| `shoal-logo.png` | Raster logo (876×240) where SVG is not accepted |
| `shoal-icon.svg` | The mark alone, transparent background |
| `shoal-icon-mono.svg` | One-colour mark; takes the surrounding text colour (`currentColor`) |

In Markdown, switch between light and dark versions with `<picture>`:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/shoal-logo-dark.svg">
  <img src="docs/assets/brand/shoal-logo.svg" alt="Shoal" width="320">
</picture>
```

Give the logo clear space at least the height of one small fish on every side. Do not recolour
individual fish, stretch the logo, add effects, or set the wordmark in another typeface.

## App icon and favicon

| File | Use |
|---|---|
| `favicon.svg` | Browser tab icon; three fish on a navy tile that darkens in dark mode |
| `favicon.ico` | Legacy browsers (16, 32 and 48 px) |
| `favicon-32.png` | PNG fallback |
| `apple-touch-icon.png` | iOS home screen (180×180) |
| `favicon-512.png` | App stores, PWA manifests, avatars (512×512) |

```html
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
```

## Social preview

`social-preview.png` (1280×640) is the image for the GitHub repository's social preview
(Settings → General → Social preview) and link cards. `social-preview.svg` is its source.

## Colours

| Name | Hex | Use |
|---|---|---|
| Deep water | `#0B2E4F` | Wordmark, dark surfaces, the favicon tile |
| Shoal teal | `#0E9F8E` | Primary brand colour, primary actions |
| Reef teal | `#0C7C8C` | Darker fish, hover states |
| Shallows | `#38BDF8` | Accents, links on dark surfaces |
| Lead coral | `#FF7A59` | The lead fish; use sparingly for highlights and calls to action |

On dark backgrounds use the lighter tints `#2DD4BF`, `#5EEAD4` and `#7DD3FC` for teal and sky.

## Name

Write **Shoal** with a capital S in prose; the wordmark is lower-case. The project began as
"AI Workflow Manager", which is why the Python package, the `awm` CLI, the `AWM_*` settings and the
`awm.dev/v1` apiVersion still carry `awm`; they will move to Shoal names in a later release.
