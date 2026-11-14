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
the visitor submits a search. The workflow precomputes sequence match counts
and review flags and publishes a compact search index and observation detail
shards. The browser downloads the index and the shards needed for the current
results; it does not build a persistent full-catalog cache. The prototype
index was about 6.2 MB compressed, with a typical 30-result search downloading
about 7 MB total before photos. Actual transfer and memory use vary by search
and browser. Observation photos remain hosted by iNaturalist and are referenced
by URL. The legacy compressed catalog remains available to the workflow for
incremental updates and to older site versions.
Each observation card can share a link that opens the explorer filtered to and
scrolled to that observation.

Run the catalog unit tests with:

```text
python -m unittest discover -s tests
```