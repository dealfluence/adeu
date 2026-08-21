# Real-Document Corpus (fetched, never committed)

This directory holds real public-sector Word documents used by the content-controls
acceptance suite ([specs/content-controls/spec-corpus.md](../../specs/content-controls/spec-corpus.md)).
Everything here except `README.md` and `manifest.json` is **gitignored** — we fetch
these documents from their official government sources instead of redistributing them.

```bash
python scripts/fetch_corpus.py            # fetch everything missing
python scripts/fetch_corpus.py --list     # show manifest keys + on-disk status
python scripts/fetch_corpus.py --only fedramp_ssp_rev4,dau_acquisition_plan
```

- Tests **skip** when a document is absent — CI stays green with an empty corpus.
- Some Canadian sources (canada.ca / gc.ca) bot-block automated downloads. The fetcher
  prints exact manual instructions for those; a manually downloaded file saved here
  under the manifest's `file` name is fully equivalent.
- Upstream revisions happen. A sha256/size drift prints a WARNING (the file is kept);
  refresh `manifest.json` (`sha256`, `bytes`, `sdt_facts`) in a small PR when that fires.
- `ADEU_CORPUS_DIR` relocates this directory for both the fetcher and the tests.
