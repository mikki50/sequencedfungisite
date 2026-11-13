# Sequenced Fungi Site

## Static catalog

The site publishes a compact iNaturalist ITS catalog to GitHub Pages. The
`Update fungal ITS catalog` GitHub Actions workflow runs on pushes to `main`,
hourly, and performs a full reconciliation weekly.
The first run seeds the catalog; it may take around an hour because it
retrieves the existing ITS records in rate-limited pages.

To enable deployment, open the repository's **Settings → Pages** and set the
build and deployment source to **GitHub Actions**. The workflow needs no
repository secrets. It publishes only the current snapshot and does not commit
hourly data files to the source branch. The workflow removes its temporary
deployment artifact after each run so hourly snapshots do not accumulate in
Actions artifact storage.

The explorer opens with blank filters and does not request observations until
the visitor submits a search. On a first search, a small preview appears while
the full catalog is cached in IndexedDB; a spinner below the filters indicates
loading. Global sequence matching is available after caching finishes. The
current snapshot downloads at about 46 MB compressed and expands to about
272 MiB of JSONL before IndexedDB overhead; actual browser storage use varies.
Later visits use the local copy and refresh it at most once every 24 hours.
Observation photos remain hosted by iNaturalist and are referenced by URL.

Run the catalog unit tests with:

```text
python -m unittest discover -s tests
```