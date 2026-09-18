# Asha launch website

A plain static site. No framework, no build step, no npm, no server functions.
Everything in this folder is exactly what gets deployed.

## Files

```
website/
  index.html          landing page (hero, how it works, why, plans, download, FAQ)
  privacy.html        plain-language privacy policy
  ai-disclosure.html  AI-generated content disclosure
  styles.css          all styling (dark + light via prefers-color-scheme)
  app.js              small progressive enhancement: mobile nav toggle only
  _headers            Cloudflare Pages caching + security headers
  assets/
    jarvis-logo.png   the brand mark
    jarvis-icon.png   favicon
```

## Deploy to Cloudflare Pages (free tier)

Cloudflare Pages is used because its free tier is launch-compatible for a commercial
product (no non-commercial clause), it accepts a private repository, and it provides
free SSL and custom domains. Vercel Hobby and GitHub Pages are disqualified for a
product launch by their own terms.

### Option A — connect the Git repository (recommended)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git**.
2. Pick the Asha repository and the production branch.
3. Build settings:
   - **Framework preset:** None
   - **Build command:** leave empty
   - **Build output directory:** `/`
   - **Root directory (advanced):** `website`

   If the deploy cannot find the files, use the alternative instead: leave
   **Root directory** empty and set **Build output directory** to `website`.
4. **Save and Deploy.** No environment variables are needed.
5. Add a custom domain under **Custom domains** (free SSL is automatic).

Every push to the production branch redeploys. Preview deployments are created for
other branches and pull requests automatically.

### Option B — direct upload (no repository link)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Upload assets**.
2. Drag the **contents of this `website/` folder** (not the folder itself) into the
   upload area, or zip and upload it.
3. Deploy.

Direct uploads are not tied to Git, so you re-upload to update. Prefer Option A for a
launch site.

### Local preview

No server is required — open `index.html` directly in a browser. For a closer
approximation of hosting (relative paths, caching headers ignored), you can serve the
folder with any static server, for example:

```bash
python3 -m http.server 8080 --directory website
```

Then open <http://127.0.0.1:8080/>. Do not use ports 7860 or 8000 (the app uses them).

## Swapping the logo

The brand mark is referenced in exactly one place: the `--logo-url` custom property at
the top of `styles.css`. Replace `assets/jarvis-logo.png` (or point that one line at a
new file) and the header, hero and footer all update together. The favicon is a separate
file, `assets/jarvis-icon.png`, referenced from each page's `<head>`.

## Things to finish before launch

- Make the GitHub repository public, otherwise the "View on GitHub" and download
  links point at a 404. Update the repository URL in `index.html`, `privacy.html`,
  `ai-disclosure.html` and `README.md` if it changes.
- Replace the placeholder release links once the tagged release exists.
- Keep the licence line in each footer in sync with the repository's MIT licence
  (currently "© 2026 Atul Jaiswal (hummingseo). Free and open source under the
  MIT licence." in each footer).
- Fill in the plan usage limits once the inference model is decided (search for
  "fair use" in `index.html`).
- The public contact channel is the email `atul.j@hummingseo.com`; the footer
  also links to GitHub issues.
