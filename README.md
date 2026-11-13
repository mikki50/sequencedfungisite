# Sequenced Fungi Site

## Static catalog

The site publishes a compact iNaturalist ITS catalog to GitHub Pages. The
`Update fungal ITS catalog` GitHub Actions workflow runs hourly, applies
incremental observation updates, and performs a full reconciliation weekly.
The first run seeds the catalog; it may take around 20 minutes because it
retrieves the existing ITS records in rate-limited pages.

To enable deployment, open the repository's **Settings → Pages** and set the
build and deployment source to **GitHub Actions**. The workflow needs no
repository secrets. It publishes only the current snapshot and does not commit
hourly data files to the source branch. The workflow removes its temporary
deployment artifact after each run so hourly snapshots do not accumulate in
Actions artifact storage.

The browser downloads the compressed catalog and stores it in IndexedDB for
local filtering. A browser refreshes its cached copy at most once every 24
hours; a new browser uses the latest published snapshot. Observation photos
remain hosted by iNaturalist and are referenced by URL.

Run the catalog unit tests with:

```text
python -m unittest discover -s tests
```