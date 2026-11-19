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

The homepage offers the explorer, review categories, taxonomy tree, sequence
timeline, and project information without navigating to separate pages. The
review categories use the same filters and results as the explorer. Existing
standalone page links remain available.

The explorer opens with blank filters and does not request observations until
the visitor submits a search or selects an observation category. Filters are
collapsed by default. The workflow precomputes sequence match counts and review flags and
publishes a compact
search index and observation detail shards. The browser downloads the index and
the shards needed for the current results; it does not build a persistent
full-catalog cache. The prototype index was about 6.2 MB compressed, with a
typical 30-result search downloading
about 7 MB total before photos. Actual transfer and memory use vary by search
and browser. Observation photos remain hosted by iNaturalist and are referenced
by URL. The legacy compressed catalog remains available to the workflow for
incremental updates and to older site versions.
Each observation card can share a link that opens the explorer filtered to and
scrolled to that observation.
Cards show a MycoMap BLAST Job link when an observation has a MycoMap BLAST
Results field. The catalog sync stores this field in SQLite and publishes it
with the observation details; cards do not make separate requests to look it up.
When the catalog data version changes, the next sync performs a full rebuild
before publishing, so existing observations receive the new fields. Increment
`CATALOG_DATA_VERSION` when adding fields that require a full backfill.
Country codes in match lists are inferred from the observation's location text
and may be unavailable.

Sequence Uploaded is the date the ITS observation field was created, not
necessarily when the DNA was sequenced. iNaturalist's bulk API does not expose
that date, so the sync retrieves up to 200 individual observations per run
(maximum 4,800 per UTC day), newest IDs first. Existing records will be
backfilled gradually; dates may initially be unavailable. Stored dates survive
incremental syncs and full reconciliations when the field value is unchanged.

Run the catalog unit tests with:

```text
python -m unittest discover -s tests
```