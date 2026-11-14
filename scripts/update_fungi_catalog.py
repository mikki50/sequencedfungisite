#!/usr/bin/env python3
"""Build a compact static catalog of iNaturalist fungal ITS observations."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

if __package__:
    from .build_search_index import build_search_index
else:
    from build_search_index import build_search_index


API_URL = "https://api.inaturalist.org/v1/observations"
PAGE_SIZE = 200
ITS_FIELD_ID = 2330
FUNGI_ICONIC_TAXON_ID = 47170
REQUEST_INTERVAL_SECONDS = 1.05
UPDATE_OVERLAP = dt.timedelta(hours=1)
USER_AGENT = (
    "SequencedFungiSiteCatalog/1.0 "
    "(https://github.com/mikki50/sequencedfungisite)"
)
SEQUENCE_PATTERN = re.compile(r"^[ACGTURYSWKMBDHVN.\-]+$")
LAST_API_REQUEST = 0.0


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def timestamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def normalize_sequence(value: Any) -> str:
    if not value:
        return ""
    sequence = re.sub(r"^>.*$", "", str(value), flags=re.MULTILINE)
    sequence = re.sub(r"\s+", "", sequence).upper()
    if len(sequence) < 100 or not SEQUENCE_PATTERN.fullmatch(sequence):
        return ""
    return sequence


def compact_observation(observation: dict[str, Any]) -> dict[str, Any] | None:
    field_value = next(
        (
            field
            for field in observation.get("ofvs", [])
            if str(field.get("field_id")) == str(ITS_FIELD_ID)
        ),
        None,
    )
    sequence = normalize_sequence(field_value.get("value") if field_value else "")
    if not sequence:
        return None

    taxon = observation.get("taxon") or {}
    user = observation.get("user") or {}
    photos = observation.get("photos") or []
    photo_url = photos[0].get("url", "") if photos else ""
    if photo_url:
        photo_url = photo_url.replace("square", "medium")

    return {
        "id": observation["id"],
        "taxon": {
            "id": taxon.get("id"),
            "name": taxon.get("name"),
            "rank": taxon.get("rank"),
            "rank_level": taxon.get("rank_level"),
            "ancestor_ids": taxon.get("ancestor_ids") or [],
        },
        "species_guess": observation.get("species_guess"),
        "observed_on": observation.get("observed_on"),
        "created_at": observation.get("created_at"),
        "updated_at": observation.get("updated_at"),
        "quality_grade": observation.get("quality_grade"),
        "place_guess": observation.get("place_guess"),
        "place_ids": observation.get("place_ids") or [],
        "project_ids": observation.get("project_ids") or [],
        "user": {
            "id": user.get("id"),
            "login": user.get("login"),
        },
        "photos": [{"medium_url": photo_url}] if photo_url else [],
        "ofvs": [{"field_id": ITS_FIELD_ID, "value": sequence}],
    }


def request_json(url: str, *, api_request: bool = False) -> dict[str, Any]:
    global LAST_API_REQUEST

    if api_request:
        delay = REQUEST_INTERVAL_SECONDS - (time.monotonic() - LAST_API_REQUEST)
        if delay > 0:
            time.sleep(delay)

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": USER_AGENT,
        },
    )

    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = response.read()
            if api_request:
                LAST_API_REQUEST = time.monotonic()
            return json.loads(body)
        except urllib.error.HTTPError as error:
            if api_request:
                LAST_API_REQUEST = time.monotonic()
            if error.code == 404:
                raise
            retry_after = error.headers.get("Retry-After")
            if error.code != 429 and error.code < 500:
                raise
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt * 5
            print(
                f"Request returned HTTP {error.code}; retrying in {wait:g}s.",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait)
        except urllib.error.URLError:
            if attempt == 5:
                raise
            time.sleep(2**attempt * 5)

    raise RuntimeError(f"Request failed after retries: {url}")


def fetch_observations(
    params: dict[str, str], *, api_url: str = API_URL
) -> Iterator[dict[str, Any]]:
    cursor = 0
    page_number = 0
    while True:
        page_params = {
            **params,
            "per_page": str(PAGE_SIZE),
            "order_by": "id",
            "order": "asc",
        }
        if cursor:
            page_params["id_above"] = str(cursor)

        url = api_url + "?" + urllib.parse.urlencode(page_params)
        response = request_json(url, api_request=True)
        records = response.get("results") or []
        if not records:
            break

        page_number += 1
        if page_number == 1 or page_number % 50 == 0:
            total = response.get("total_results", "unknown")
            print(
                f"Fetched page {page_number}; {total} observations match.",
                flush=True,
            )

        for record in records:
            yield record

        next_cursor = max(int(record["id"]) for record in records)
        if next_cursor <= cursor:
            raise RuntimeError("iNaturalist pagination did not advance.")
        cursor = next_cursor
        if len(records) < PAGE_SIZE:
            break


def connect_catalog(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS observations "
        "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
    )
    return connection


def store_observations(
    connection: sqlite3.Connection,
    records: Iterator[dict[str, Any]],
    *,
    replace_catalog: bool,
) -> tuple[int, int]:
    if replace_catalog:
        connection.execute("DELETE FROM observations")

    changed = 0
    accepted = 0
    for observation in records:
        changed += 1
        compact = compact_observation(observation)
        observation_id = int(observation["id"])
        if compact is None:
            connection.execute(
                "DELETE FROM observations WHERE id = ?", (observation_id,)
            )
        else:
            connection.execute(
                "INSERT INTO observations (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (
                    observation_id,
                    json.dumps(compact, separators=(",", ":"), ensure_ascii=False),
                ),
            )
            accepted += 1

        if changed % 5000 == 0:
            connection.commit()
            print(f"Processed {changed} changed observations.", flush=True)

    connection.commit()
    return changed, accepted


def download_previous_catalog(
    base_url: str, connection: sqlite3.Connection
) -> dict[str, Any] | None:
    base_url = base_url.rstrip("/") + "/"
    try:
        manifest = request_json(base_url + "manifest.json")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise

    catalog_url = urllib.parse.urljoin(base_url, manifest["catalog_file"])
    request = urllib.request.Request(
        catalog_url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        with gzip.GzipFile(fileobj=response) as catalog:
            batch: list[tuple[int, str]] = []
            for line in catalog:
                if not line.strip():
                    continue
                record = json.loads(line)
                batch.append(
                    (
                        int(record["id"]),
                        json.dumps(record, separators=(",", ":"), ensure_ascii=False),
                    )
                )
                if len(batch) >= 5000:
                    connection.executemany(
                        "INSERT INTO observations (id, payload) VALUES (?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                        batch,
                    )
                    connection.commit()
                    batch.clear()
            if batch:
                connection.executemany(
                    "INSERT INTO observations (id, payload) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                    batch,
                )
    connection.commit()
    print(
        f"Loaded {connection.execute('SELECT COUNT(*) FROM observations').fetchone()[0]} "
        "records from the published catalog.",
        flush=True,
    )
    return manifest


def write_catalog(connection: sqlite3.Connection, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary_path.open("wb") as raw_file:
        with gzip.GzipFile(
            fileobj=raw_file,
            mode="wb",
            compresslevel=6,
            mtime=0,
        ) as compressed:
            cursor = connection.execute(
                "SELECT payload FROM observations ORDER BY id DESC"
            )
            count = 0
            for (payload,) in cursor:
                compressed.write(payload.encode("utf-8") + b"\n")
                count += 1
    temporary_path.replace(output_path)
    return count


def build_catalog(
    output_dir: Path,
    *,
    base_url: str,
    force_full: bool,
    api_url: str = API_URL,
) -> dict[str, Any]:
    started_at = utc_now()
    version = os.environ.get("GITHUB_RUN_ID") or started_at.strftime("%Y%m%dT%H%M%SZ")
    index_dir = f"search-{version}"
    with tempfile.TemporaryDirectory(prefix="fungi-catalog-") as temp_dir:
        connection = connect_catalog(Path(temp_dir) / "catalog.sqlite")
        previous_manifest = None
        if not force_full:
            previous_manifest = download_previous_catalog(base_url, connection)

        full_refresh = force_full or previous_manifest is None
        if full_refresh:
            params = {
                "iconic_taxa": str(FUNGI_ICONIC_TAXON_ID),
                "field:DNA Barcode ITS": "",
            }
            mode = "full"
        else:
            last_sync = dt.datetime.fromisoformat(
                previous_manifest["last_successful_sync"].replace("Z", "+00:00")
            )
            updated_since = timestamp(last_sync - UPDATE_OVERLAP)
            params = {
                "iconic_taxa": str(FUNGI_ICONIC_TAXON_ID),
                "updated_since": updated_since,
            }
            mode = "incremental"
            print(f"Syncing fungal observations updated since {updated_since}.", flush=True)

        changed, accepted = store_observations(
            connection,
            fetch_observations(params, api_url=api_url),
            replace_catalog=full_refresh,
        )
        catalog_file = "observations.jsonl.gz"
        record_count = write_catalog(
            connection, output_dir / "data" / catalog_file
        )
        index_count = build_search_index(connection, output_dir / "data" / index_dir)
        if index_count != record_count:
            raise RuntimeError("Search index and catalog record counts do not match.")

    manifest = {
        "schema_version": 1,
        "catalog_version": version,
        "catalog_file": catalog_file,
        "search_index": f"{index_dir}/index.json.gz",
        "records": record_count,
        "generated_at": timestamp(utc_now()),
        "last_successful_sync": timestamp(started_at),
        "sync_mode": mode,
        "observations_scanned": changed,
        "valid_its_records_seen": accepted,
        "field_id": ITS_FIELD_ID,
        "taxon_id": FUNGI_ICONIC_TAXON_ID,
    }
    manifest_path = output_dir / "data" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2), flush=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--base-url",
        default="https://mikki50.github.io/sequencedfungisite/data/",
    )
    parser.add_argument("--force-full", action="store_true")
    args = parser.parse_args()

    build_catalog(
        args.output_dir,
        base_url=args.base_url,
        force_full=args.force_full,
    )


if __name__ == "__main__":
    main()
