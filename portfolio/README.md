# Reelwave Portfolio Case Study

This is a dependency-free static site designed for GitHub Pages. It is intentionally isolated from the Reelwave local Alpha and does not read the database or `data/` directory.

## Local preview

From the repository root:

```bash
python3 -m http.server 4173 --directory portfolio
```

Then open `http://127.0.0.1:4173/`.

## GitHub Pages

The workflow at `.github/workflows/deploy-portfolio.yml` deploys only the `portfolio/` directory. In the GitHub repository, open **Settings → Pages** and set **Source** to **GitHub Actions**. Push to `main` or run the workflow manually.

All site asset URLs are relative (`./...`), so the page works under a repository subpath such as `/reelsift/` without a hard-coded base URL.

## Real media checklist

See `assets/README.md`. The current interactive Story Change sequence is explicitly labeled as a deterministic portfolio explainer; it does not claim to be a live Reelwave AI response.
