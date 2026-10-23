# blocker-component-index
Evidence index of Android SDK components (which SDK declares which component), feeding blocker-general-rules

## How it works

A weekly GitHub Actions job (`.github/workflows/crawl.yml`) runs `scripts/crawl.py all`:

1. **discover**: builtin repositories (Google Maven, Maven Central), `config/seeds.yaml`, Maven repository URLs found in Android Gradle builds on GitHub code search, and `<repositories>` in fetched POMs.
2. **probe**: detects how each repository can be listed (Google index, Central search, Nexus 2/3, Artifactory, directory listing, or metadata only). Proxy mirrors of Central are detected and skipped.
3. **enumerate**: lists artifacts; repositories that cannot be listed are queried with coordinates harvested from GitHub builds.
4. **fetch / extract**: reads only `AndroidManifest.xml` from each AAR (HTTP Range when supported) and records declared components in `data/artifacts/<groupId>/<artifactId>.json`.
5. **report**: `reports/coverage.md` compares components against blocker-general-rules keywords.

Runs are resumable: progress lives in `data/state/`, and each run stops cleanly at `--max-minutes`.

```bash
python -m pip install -r scripts/requirements.txt
python -m unittest discover -s scripts
GITHUB_TOKEN=... python scripts/crawl.py all --max-minutes 60 --rules-dir ../blocker-general-rules
```

Code search needs a token. The workflow uses the `CODE_SEARCH_TOKEN` secret (fine-grained PAT, public read-only) if set, otherwise `GITHUB_TOKEN`.
