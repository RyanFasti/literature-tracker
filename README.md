# Literature Tracker — Step 4: weekly automated collection + free public website

**Scope:** This adds a scheduled GitHub Actions workflow that updates a **read-only public catalog** weekly and deploys it to GitHub Pages. It does **not** put your private SQLite database online or synchronize colleagues' ratings, notes, acknowledgment, or hidden status. The local dashboard (`python -m collector serve`) remains unchanged.

**Requirements:** Python 3.10+; a GitHub account and **public** repository for the free GitHub Pages setup. The collector and tests use only the Python standard library. Hosting and automation are subject to GitHub's free-tier limits and service policies. No paid services or API keys are required for the basic weekly catalog.

## 1. Install on your existing Windows project

Open PowerShell and run **from the project root, not `docs`**:

```powershell
cd C:\Users\ryanf\LiteratureApp\literature-tracker-step1
Copy-Item .\data\literature.sqlite3 ".\data\literature-backup-$(Get-Date -Format yyyyMMdd-HHmmss).sqlite3"
Expand-Archive -Path "$HOME\Downloads\literature-tracker-step4-patch.zip" -DestinationPath . -Force
python -m unittest discover -s tests -v
```

Expected offline result: **76 tests, OK**. The patch contains **no `data/`, SQLite files, `docs/`, or credentials**. It adds the GitHub Actions workflow, the public-catalog merge/validation module, tests, and a public exclusion list.

## 2. Prepare and review the initial public catalog

```powershell
python -m collector build-site --output docs
python -m collector check-public --path docs/articles.json
```

Open `docs/articles.json` in a text editor and review what will become public. The catalog contains included/review metadata, **including manually added DOIs**; by default it contains **no abstracts, private notes, ratings, acknowledgments, hidden flags, SQLite database, or rejected-paper archive**. If a DOI should not be public, add it to `config/public-exclusions.json`, for example:

```json
{"dois": ["10.1000/example"]}
```

The exclusions apply during the weekly merge. To remove a DOI **before first publication**, add it to the exclusions file, then apply the exclusions to your initial catalog **before committing**:

```powershell
python -m collector merge-site --previous docs/articles.json --incoming docs/articles.json --output docs/articles.json --exclusions config/public-exclusions.json
python -m collector check-public --path docs/articles.json --min-articles 1
```
 A DOI already committed to a public Git repository can remain visible in its Git history, even if later excluded; never publish sensitive data expecting easy deletion.

## 3. Create a public GitHub repository

Create an **empty public** repository in GitHub, for example `literature-tracker`, without adding a generated README. If Git is installed, run from the project root, replacing `YOUR_GITHUB_USERNAME`:

```powershell
git init
git branch -M main
git add -- .gitignore README.md collector/ config/ site/ web/ tests/ .github/ docs/index.html docs/app.js docs/styles.css docs/articles.json docs/.nojekyll
git status --short
git commit -m "Initial public literature tracker"
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/literature-tracker.git
git push -u origin main
```

**Inspect `git status` before committing.** Do not commit `data/`, database backups, `.env` files, or any extra folders accidentally extracted into `docs/`. The instructions deliberately add only the five public site files from `docs/`. If Git is not installed, GitHub Desktop can initialize and publish the repository, but you must still inspect the files being committed.

In the GitHub repository, open **Settings → Pages → Build and deployment** and select **GitHub Actions** as the source. If a workflow push fails with HTTP 403, inspect **Settings → Actions → General → Workflow permissions** and repository branch protections; the workflow needs permission to commit changes to the default branch. Never add a personal access token to source files.

## 4. Trigger the first deployment

Open **Actions → Weekly public literature catalog → Run workflow** on GitHub. The workflow:

1. Runs all offline tests and validates the already-committed `docs/articles.json` (it refuses to start if the initial catalog is absent).
2. Uses a fresh **temporary** SQLite database to collect all six journals over an overlapping **45-day window**, including Crossref publication, online, creation, and update-date passes.
3. Builds new public metadata, merges it with the previously published DOI catalog, and preserves all older published papers unless their DOI is in `config/public-exclusions.json`. New metadata may promote Review to Include; it never silently demotes an existing Include.
4. Refuses publication if the JSON contains abstracts, private state, duplicate DOIs, unexpected fields, or invalid classifications.
5. Commits **only** `docs/index.html`, `docs/app.js`, `docs/styles.css`, `docs/articles.json`, and `docs/.nojekyll`. It deploys only those allowlisted files to GitHub Pages.

The schedule is **Monday 07:17 UTC**. GitHub may delay or disable scheduled workflows; monitor the Actions tab periodically. If any journal collection fails, the workflow stops without publishing a partial update. The public site will normally appear at `https://YOUR_GITHUB_USERNAME.github.io/literature-tracker/` once GitHub Pages reports deployment success. The actual URL is shown in the workflow's deployment details.

## 5. Local-only checks and public DOI removal

Validate your public JSON at any time:

```powershell
python -m collector check-public --path docs/articles.json
```

To remove a DOI from future public snapshots, add it to `config/public-exclusions.json`, commit that file, and manually rerun the workflow. This affects only the public catalog; your local SQLite records are not deleted. A public Git commit history may still contain earlier versions.

To test the merge locally without network access, create a second `articles.json` via `build-site` or use the existing one:

```powershell
python -m collector merge-site --previous docs/articles.json --incoming docs/articles.json --output docs/merged-preview.json --exclusions config/public-exclusions.json
python -m collector check-public --path docs/merged-preview.json
```

`docs/merged-preview.json` is a temporary local file: do not commit it. Delete it after reviewing.

## Scientific and architectural limitations

- The hosted catalog is **read-only** and has no login. Per-user ratings, notes, acknowledgment, hiding, and shared synchronization remain **local-only** until a separate authenticated backend is implemented.
- A 45-day rolling query is not a proof of exhaustive journal coverage. Metadata can arrive later, sources can disagree on dates, and the classifier can produce false positives and negatives. Run periodic independent coverage and rejected-paper audits on the local database.
- The scheduled runner's temporary SQLite file and rejected-paper archive are discarded after each run. Historical **publicly included/review** DOIs persist through `docs/articles.json`, not through a cloud database. Your local full archive remains separate.
- The workflow publishes new Include and Review papers automatically, without human validation. Review labels remain visible; do not present these as peer-reviewed relevance decisions.
- The default static site intentionally excludes abstracts because redistribution rights have not been established for every publisher.
- Publishing the repository is a separate, explicit user action. Installing this patch does **not** create a GitHub repository, upload data, or make the local dashboard publicly accessible.

---

## Prior steps

Step 3.2 recovered the three EES papers with year-only publication dates and metadata redeposits in the October 19–November 18, 2026 window. The subsequent OpenAlex comparison accounted for 2,604 of 2,606 indexed DOIs across six journals (99.92% index agreement); the remaining two were unrelated Zenodo records assigned to Science Advances. This does not establish publisher completeness or classifier recall.
