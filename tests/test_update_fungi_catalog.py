import datetime as dt
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.update_fungi_catalog import (
    CATALOG_DATA_VERSION,
    backfill_sequence_dates,
    build_catalog,
    compact_observation,
    connect_catalog,
    download_previous_catalog,
    normalize_sequence,
    store_observations,
    write_catalog,
)
from scripts.build_search_index import build_search_index
from scripts.build_country_map import build_country_map, place_metadata
from scripts.build_admin_boundaries import paths as admin_boundary_paths
from scripts.build_taxonomy import build_taxonomy


class NormalizeSequenceTests(unittest.TestCase):
    def test_removes_fasta_headers_and_whitespace(self):
        sequence = ">sample name\nacgt acgt\n" + "N" * 100
        self.assertEqual(normalize_sequence(sequence), "ACGTACGT" + "N" * 100)

    def test_rejects_short_or_invalid_sequences(self):
        self.assertEqual(normalize_sequence("ACGT" * 20), "")
        self.assertEqual(normalize_sequence("A" * 99 + "Z"), "")


class CompactObservationTests(unittest.TestCase):
    def test_keeps_only_search_and_card_fields(self):
        sequence = "ACGT" * 30
        compact = compact_observation({
            "id": 12,
            "taxon": {
                "id": 34,
                "name": "Agaricus example",
                "rank": "species",
                "rank_level": 10,
                "ancestor_ids": [1, 2],
            },
            "species_guess": "Agaricus example",
            "observed_on": "2026-01-02",
            "created_at": "2026-01-03T00:00:00Z",
            "updated_at": "2026-01-04T00:00:00Z",
            "quality_grade": "research",
            "place_guess": "Example place",
            "place_ids": [6744],
            "project_ids": [56],
            "user": {"id": 78, "login": "observer"},
            "photos": [{"url": "https://example.org/square.jpg"}],
            "ofvs": [
                {"field_id": 2330, "value": sequence},
                {"field_id": 9864, "value": "https://mycomap.com/genetics/blast-search/example/"},
            ],
            "description": "not retained",
            "location": "not retained",
        })

        self.assertIsNotNone(compact)
        assert compact is not None
        self.assertEqual(compact["id"], 12)
        self.assertEqual(compact["photos"][0]["medium_url"], "https://example.org/medium.jpg")
        self.assertEqual(compact["ofvs"][0]["value"], sequence)
        self.assertEqual(compact["mycoblast_url"], "https://mycomap.com/genetics/blast-search/example/")
        self.assertNotIn("description", compact)
        self.assertNotIn("location", compact)

    def test_records_absent_mycoblast_link(self):
        compact = compact_observation({
            "id": 12,
            "ofvs": [{"field_id": 2330, "value": "ACGT" * 30}],
        })
        self.assertIsNotNone(compact)
        self.assertIsNone(compact["mycoblast_url"])

    def test_skips_observations_without_a_valid_its_value(self):
        observation = {
            "id": 12,
            "ofvs": [{"field_id": 2330, "value": "too short"}],
        }
        self.assertIsNone(compact_observation(observation))


class WriteCatalogTests(unittest.TestCase):
    def test_writes_newest_observation_ids_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            connection.executemany(
                "INSERT INTO observations (id, payload) VALUES (?, ?)",
                [
                    (1, json.dumps({"id": 1})),
                    (2, json.dumps({"id": 2})),
                ],
            )
            connection.commit()

            output_path = root / "data" / "observations.jsonl.gz"
            self.assertEqual(write_catalog(connection, output_path), 2)

            with gzip.open(output_path, "rt", encoding="utf-8") as catalog:
                observation_ids = [
                    json.loads(line)["id"]
                    for line in catalog
                ]

            self.assertEqual(observation_ids, [2, 1])
            connection.close()


class CatalogSyncTests(unittest.TestCase):
    def test_outdated_manifest_does_not_download_old_catalog(self):
        manifest = {"schema_version": 1}
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = connect_catalog(Path(temp_dir) / "catalog.sqlite")
            with patch("scripts.update_fungi_catalog.request_json", return_value=manifest), \
                 patch("scripts.update_fungi_catalog.urllib.request.urlopen") as download:
                self.assertEqual(download_previous_catalog("https://example.org/data", connection), manifest)
                download.assert_not_called()
            connection.close()

    def test_data_version_change_rebuilds_and_current_version_syncs_incrementally(self):
        sequence = "ACGT" * 30
        url = "https://mycomap.com/genetics/blast-search/example/"
        record = {"id": 12, "ofvs": [
            {"field_id": 2330, "value": sequence},
            {"field_id": 9864, "value": url},
        ]}
        for previous_version, expected_mode in [(None, "full"), (CATALOG_DATA_VERSION, "incremental")]:
            with self.subTest(mode=expected_mode), tempfile.TemporaryDirectory() as temp_dir:
                manifest = {
                    "last_successful_sync": "2026-01-01T00:00:00Z",
                    "catalog_data_version": previous_version,
                }

                def load_previous(base_url, connection):
                    old_record = {"id": 1, "ofvs": [{"field_id": 2330, "value": sequence}]}
                    connection.execute(
                        "INSERT INTO observations (id, payload) VALUES (?, ?)",
                        (old_record["id"], json.dumps(old_record)),
                    )
                    return manifest

                with patch("scripts.update_fungi_catalog.download_previous_catalog", side_effect=load_previous), \
                     patch("scripts.update_fungi_catalog.fetch_observations", return_value=iter([record])) as fetch, \
                     patch("scripts.update_fungi_catalog.backfill_sequence_dates", return_value=0), \
                     patch("scripts.update_fungi_catalog.build_taxonomy"):
                    result = build_catalog(Path(temp_dir), base_url="https://example.org/data", force_full=False)

                self.assertEqual(result["sync_mode"], expected_mode)
                self.assertEqual(result["catalog_data_version"], CATALOG_DATA_VERSION)
                params = fetch.call_args.args[0]
                self.assertEqual("updated_since" in params, expected_mode == "incremental")
                with gzip.open(Path(temp_dir) / "data" / "observations.jsonl.gz", "rt", encoding="utf-8") as file:
                    records = [json.loads(line) for line in file]
                self.assertEqual(len(records), 1 if expected_mode == "full" else 2)
                self.assertEqual(next(item for item in records if item["id"] == 12)["mycoblast_url"], url)
                with gzip.open(Path(temp_dir) / "data" / result["search_index"].replace("index.json.gz", "details/0.json.gz"), "rt", encoding="utf-8") as file:
                    details = json.load(file)
                self.assertEqual(next(item for item in details if item["id"] == 12)["mycoblast_url"], url)
                with gzip.open(Path(temp_dir) / "data" / result["country_map"], "rt", encoding="utf-8") as file:
                    self.assertEqual(json.load(file)["total"], len(records))

    def test_sequence_dates_survive_sync_only_for_the_same_field(self):
        sequence = "ACGT" * 30
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = connect_catalog(Path(temp_dir) / "catalog.sqlite")
            old = [
                {"id": 1, "sequence_field_value_id": 10, "sequence_uploaded_at": "2026-02-21T02:09:46Z"},
                {"id": 2, "sequence_field_value_id": 20, "sequence_uploaded_at": "2026-02-21T02:09:46Z"},
                {"id": 3, "sequence_field_value_id": 30, "sequence_uploaded_at": "2026-02-21T02:09:46Z"},
            ]
            connection.executemany(
                "INSERT INTO observations (id, payload) VALUES (?, ?)",
                [(record["id"], json.dumps(record)) for record in old],
            )
            incoming = iter([
                {"id": 1, "ofvs": [{"id": 10, "field_id": 2330, "value": sequence}]},
                {"id": 2, "ofvs": [{"id": 21, "field_id": 2330, "value": sequence}]},
            ])
            store_observations(connection, incoming, replace_catalog=True)
            rows = {record["id"]: record for (payload,) in connection.execute("SELECT payload FROM observations")
                    for record in [json.loads(payload)]}
            self.assertEqual(set(rows), {1, 2})
            self.assertEqual(rows[1]["sequence_uploaded_at"], "2026-02-21T02:09:46Z")
            self.assertNotIn("sequence_uploaded_at", rows[2])
            connection.close()

    def test_backfill_uses_matching_field_and_skips_checked_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = connect_catalog(Path(temp_dir) / "catalog.sqlite")
            records = [
                {"id": 1, "sequence_field_value_id": 10, "sequence_uploaded_at": None},
                {"id": 2, "sequence_field_value_id": 20},
                {"id": 3, "sequence_field_value_id": 30},
            ]
            connection.executemany(
                "INSERT INTO observations (id, payload) VALUES (?, ?)",
                [(record["id"], json.dumps(record)) for record in records],
            )
            responses = [
                {"id": 3, "observation_field_values": [
                    {"id": 30, "observation_field_id": 2330, "created_at": "2026-02-21T02:09:46Z"}]},
                {"id": 2, "observation_field_values": [
                    {"id": 99, "observation_field_id": 2330, "created_at": "2026-02-22T00:00:00Z"}]},
            ]
            with patch("scripts.update_fungi_catalog.request_json", side_effect=responses) as request:
                self.assertEqual(backfill_sequence_dates(connection, 2), 2)
            self.assertEqual(request.call_count, 2)
            rows = {record["id"]: record for (payload,) in connection.execute("SELECT payload FROM observations")
                    for record in [json.loads(payload)]}
            self.assertEqual(rows[3]["sequence_uploaded_at"], "2026-02-21T02:09:46Z")
            self.assertIsNone(rows[2]["sequence_uploaded_at"])
            self.assertIsNone(rows[1]["sequence_uploaded_at"])
            connection.close()

    def test_backfill_recovers_field_id_for_older_catalog_records(self):
        sequence = "ACGT" * 30
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = connect_catalog(Path(temp_dir) / "catalog.sqlite")
            record = {"id": 12, "ofvs": [{"field_id": 2330, "value": sequence}]}
            connection.execute("INSERT INTO observations VALUES (?, ?)", (12, json.dumps(record)))
            response = {"id": 12, "observation_field_values": [
                {"id": 20, "observation_field_id": 2330, "value": sequence,
                 "created_at": "2026-02-21T02:09:46Z"},
            ]}
            with patch("scripts.update_fungi_catalog.request_json", return_value=response):
                self.assertEqual(backfill_sequence_dates(connection, 1), 1)
            stored = json.loads(connection.execute("SELECT payload FROM observations").fetchone()[0])
            self.assertEqual(stored["sequence_field_value_id"], 20)
            self.assertEqual(stored["sequence_uploaded_at"], "2026-02-21T02:09:46Z")
            connection.close()

    def test_sequence_date_requests_obey_daily_cap(self):
        manifest = {
            "last_successful_sync": "2026-01-01T00:00:00Z",
            "catalog_data_version": CATALOG_DATA_VERSION,
            "sequence_date_day": "2026-01-01",
            "sequence_date_requests": 4750,
        }
        now = dt.datetime.fromisoformat("2026-01-01T12:00:00+00:00")
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch("scripts.update_fungi_catalog.utc_now", return_value=now), \
             patch("scripts.update_fungi_catalog.download_previous_catalog", return_value=manifest), \
             patch("scripts.update_fungi_catalog.fetch_observations", return_value=iter([])), \
             patch("scripts.update_fungi_catalog.backfill_sequence_dates", return_value=50) as backfill, \
             patch("scripts.update_fungi_catalog.build_taxonomy"):
            result = build_catalog(Path(temp_dir), base_url="https://example.org/data", force_full=False)
        self.assertEqual(backfill.call_args.args[1], 50)
        self.assertEqual(result["sequence_date_requests"], 4800)


class TaxonomyTests(unittest.TestCase):
    def test_builds_tree_counts_and_reports_unplaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            observations = [
                {"id": 1, "taxon": {"id": 3, "ancestor_ids": [47170, 2, 3]}},
                {"id": 2, "taxon": {"id": 2, "ancestor_ids": [47170, 2]}},
                {"id": 3, "taxon": None},
            ]
            connection.executemany("INSERT INTO observations VALUES (?, ?)",
                                   [(item["id"], json.dumps(item)) for item in observations])
            taxa = [
                {"id": 47170, "name": "Fungi", "rank": "kingdom", "ancestor_ids": []},
                {"id": 2, "name": "Genus", "rank": "genus", "ancestor_ids": [47170]},
                {"id": 3, "name": "Genus species", "rank": "species", "ancestor_ids": [47170, 2]},
            ]
            with patch("scripts.build_taxonomy.TAXA_BATCH_SIZE", 200):
                build_taxonomy(connection, root, lambda url, **kwargs: {"results": taxa})
            with gzip.open(root / "taxonomy.json.gz", "rt", encoding="utf-8") as file:
                result = json.load(file)
            nodes = {item["id"]: item for item in result["nodes"]}
            self.assertEqual((result["total"], result["unplaced"]), (3, 1))
            self.assertEqual((nodes[47170]["count"], nodes[2]["count"], nodes[3]["count"]), (2, 2, 1))
            self.assertEqual((nodes[2]["direct"], nodes[3]["direct"]), (1, 1))
            self.assertEqual(nodes[3]["parent"], 2)
            connection.close()

    def test_missing_taxon_is_labelled_and_uses_recorded_ancestry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            record = {"id": 1, "taxon": {"id": 99, "ancestor_ids": [47170, 99]}}
            connection.execute("INSERT INTO observations VALUES (?, ?)", (1, json.dumps(record)))
            response = {"results": [
                {"id": 47170, "name": "Fungi", "rank": "kingdom", "ancestor_ids": []},
            ]}
            build_taxonomy(connection, root, lambda url, **kwargs: response)
            with gzip.open(root / "taxonomy.json.gz", "rt", encoding="utf-8") as file:
                data = json.load(file)
            node = next(item for item in data["nodes"] if item["id"] == 99)
            self.assertEqual((node["name"], node["parent"], node["count"]), ("Unknown taxon #99", 47170, 1))
            self.assertEqual(data["missing_taxa"], 1)
            connection.close()

    def test_reuses_previously_published_taxon_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            record = {"id": 1, "taxon": {"id": 2, "ancestor_ids": [47170, 2]}}
            connection.execute("INSERT INTO observations VALUES (?, ?)", (1, json.dumps(record)))
            previous = root / "previous.json.gz"
            with gzip.open(previous, "wt", encoding="utf-8") as file:
                json.dump({"schema": 1, "taxa": [
                    {"id": 47170, "name": "Fungi", "rank": "kingdom", "ancestor_ids": []},
                    {"id": 2, "name": "Genus", "rank": "genus", "ancestor_ids": [47170]},
                ]}, file)
            with patch("scripts.build_taxonomy.urllib.request.urlopen", return_value=previous.open("rb")):
                with patch("scripts.build_taxonomy.urllib.parse.urlencode", side_effect=AssertionError("unexpected API call")):
                    build_taxonomy(connection, root, lambda url, **kwargs: None, "https://example.org/taxonomy.gz")
            with gzip.open(root / "taxonomy.json.gz", "rt", encoding="utf-8") as file:
                data = json.load(file)
            self.assertEqual(len(data["nodes"]), 2)
            self.assertEqual(data["missing_taxa"], 0)
            connection.close()


class CountryMapTests(unittest.TestCase):
    def test_puerto_rico_has_its_own_map_entry_and_subdivisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            places = [[6848, 11627], [6848], [1], [1, 6848, 11627], []]
            connection.executemany(
                "INSERT INTO observations VALUES (?, ?)",
                [(i, json.dumps({"id": i, "place_ids": ids})) for i, ids in enumerate(places, 1)],
            )
            response = {"results": [
                {"id": 1, "admin_level": 0},
                {"id": 6848, "admin_level": 0},
                {"id": 11627, "name": "Aguadilla", "admin_level": 10,
                 "ancestor_place_ids": [6848], "location": "18.48,-67.14"},
            ]}
            build_country_map(connection, root / "out", lambda url, **kwargs: response)
            with gzip.open(root / "out" / "country-map.json.gz", "rt", encoding="utf-8") as file:
                data = json.load(file)
            countries = {country["id"]: country for country in data["countries"]}
            self.assertEqual((countries[6848]["name"], countries[6848]["count"]), ("Puerto Rico", 3))
            self.assertEqual(countries[6848]["regions"][0]["count"], 2)
            self.assertEqual(countries[6848]["unassigned_regions"], 1)
            self.assertEqual(countries[1]["count"], 2)
            self.assertEqual(data["unassigned"], 1)
            connection.close()

    def test_boundary_lines_split_at_the_antimeridian(self):
        source = {"features": [{
            "properties": {"featurecla": "Admin-1 boundary"},
            "geometry": {"type": "LineString", "coordinates": [
                [170, 1], [179, 2], [-179, 3], [-170, 4]
            ]},
        }, {
            "properties": {"featurecla": "Admin-1 boundary indicator"},
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        }]}
        segments = list(admin_boundary_paths(source))
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(abs(segment[-1][0] - segment[0][0]) < 30 for segment in segments))

    def test_antimeridian_region_center_and_missing_position(self):
        country = {1: {"id": 1}}
        region = {
            "id": 10, "name": "Islands", "admin_level": 10,
            "ancestor_place_ids": [1], "location": "2,0",
            "bounding_box_geojson": {"coordinates": [[
                [170, -2], [170, 6], [-170, 6], [-170, -2], [170, -2]
            ]]},
        }
        self.assertEqual(place_metadata(region, country)["lon"], -180)
        region["location"] = None
        metadata = place_metadata(region, country)
        self.assertIsNone(metadata["lat"])
        self.assertIsNone(metadata["lon"])

    def test_country_and_region_counts_follow_matching_place_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            countries = [
                {"id": 1, "name": "First", "lat": 10, "lon": 20},
                {"id": 2, "name": "Second", "lat": -10, "lon": -20},
            ]
            country_file = root / "countries.json"
            country_file.write_text(json.dumps(countries), encoding="utf-8")
            places = [[1, 1, 10, 10, 99], [2, 20], [1, 2, 10, 20], [10], [], [1, 20]]
            records = [
                {"id": index, "place_ids": ids}
                for index, ids in enumerate(places, 1)
            ]
            connection.executemany(
                "INSERT INTO observations (id,payload) VALUES (?,?)",
                [(record["id"], json.dumps(record)) for record in records],
            )
            response = {"results": [
                {"id": 1, "admin_level": 0},
                {"id": 2, "admin_level": 0},
                {"id": 10, "name": "Region A", "admin_level": 10,
                 "ancestor_place_ids": [1], "location": "11,21"},
                {"id": 20, "name": "Region B", "admin_level": 10,
                 "ancestor_place_ids": [2], "location": "-11,-21"},
                {"id": 99, "admin_level": None},
            ]}
            with patch("scripts.build_country_map.COUNTRY_PLACES_PATH", country_file):
                build_country_map(connection, root / "search", lambda url, **kwargs: response)
            with gzip.open(root / "search" / "country-map.json.gz", "rt", encoding="utf-8") as file:
                data = json.load(file)
            self.assertEqual((data["total"], data["unassigned"], data["missing_places"]), (6, 2, 0))
            first, second = data["countries"]
            self.assertEqual((first["count"], first["unassigned_regions"]), (3, 1))
            self.assertEqual((second["count"], second["unassigned_regions"]), (2, 0))
            self.assertEqual(first["regions"], [
                {"id": 10, "name": "Region A", "lat": 11.0, "lon": 21.0, "count": 2},
            ])
            self.assertEqual(second["regions"][0]["count"], 2)
            connection.close()

    def test_reuses_cache_and_retries_missing_places(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            connection.execute("INSERT INTO observations VALUES (?, ?)",
                               (1, json.dumps({"id": 1, "place_ids": [1, 10, 99]})))
            country_file = root / "countries.json"
            country_file.write_text(json.dumps([{"id": 1, "name": "First", "lat": 0, "lon": 0}]),
                                    encoding="utf-8")
            previous = root / "previous.json.gz"
            with gzip.open(previous, "wt", encoding="utf-8") as file:
                json.dump({"schema": 1, "country_ids": [1], "places": {
                    "1": {"bounds": None},
                    "10": {"name": "Region", "lat": 1, "lon": 2, "countries": [1]},
                }}, file)
            requests = []
            def request(url, **kwargs):
                requests.append(url)
                return {"results": []}
            with patch("scripts.build_country_map.COUNTRY_PLACES_PATH", country_file), \
                 patch("scripts.build_country_map.urllib.request.urlopen", return_value=previous.open("rb")):
                build_country_map(connection, root / "out", request, "https://example.org/places.gz")
            self.assertEqual(requests, ["https://api.inaturalist.org/v1/places/99"])
            with gzip.open(root / "out" / "country-map.json.gz", "rt", encoding="utf-8") as file:
                result = json.load(file)
            self.assertEqual(result["missing_places"], 1)
            self.assertEqual(result["countries"][0]["regions"][0]["count"], 1)
            with gzip.open(root / "out" / "place-metadata.json.gz", "rt", encoding="utf-8") as file:
                cache = json.load(file)
            self.assertNotIn("99", cache["places"])
            connection.close()

    def test_country_snapshot_change_refreshes_cached_place_ancestry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            connection.execute("INSERT INTO observations VALUES (?, ?)",
                               (1, json.dumps({"id": 1, "place_ids": [1, 10]})))
            country_file = root / "countries.json"
            country_file.write_text(json.dumps([{"id": 1, "name": "First", "lat": 0, "lon": 0}]),
                                    encoding="utf-8")
            previous = root / "previous.json.gz"
            with gzip.open(previous, "wt", encoding="utf-8") as file:
                json.dump({"schema": 1, "country_ids": [], "places": {"1": None, "10": None}}, file)
            requests = []
            def request(url, **kwargs):
                requests.append(url)
                return {"results": [
                    {"id": 1, "admin_level": 0},
                    {"id": 10, "name": "Region", "admin_level": 10,
                     "ancestor_place_ids": [1], "location": "0,0"},
                ]}
            with patch("scripts.build_country_map.COUNTRY_PLACES_PATH", country_file), \
                 patch("scripts.build_country_map.urllib.request.urlopen", return_value=previous.open("rb")):
                build_country_map(connection, root / "out", request, "https://example.org/places.gz")
            self.assertEqual(requests, ["https://api.inaturalist.org/v1/places/1,10"])
            with gzip.open(root / "out" / "country-map.json.gz", "rt", encoding="utf-8") as file:
                self.assertEqual(json.load(file)["countries"][0]["regions"][0]["count"], 1)
            connection.close()


class SearchIndexTests(unittest.TestCase):
    def test_timeline_counts_dated_observations_by_month(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            sequence = "ACGT" * 30
            records = [
                {"id": 1, "sequence_uploaded_at": "2025-02-01T00:00:00Z"},
                {"id": 2, "sequence_uploaded_at": "2025-03-01T00:30:00+02:00"},
                {"id": 3, "sequence_uploaded_at": "2025-04-03T00:00:00Z"},
                {"id": 4, "sequence_uploaded_at": None},
                {"id": 5},
            ]
            for record in records:
                record["ofvs"] = [{"field_id": 2330, "value": sequence}]
            connection.executemany(
                "INSERT INTO observations (id,payload) VALUES (?,?)",
                [(record["id"], json.dumps(record)) for record in records],
            )
            build_search_index(connection, root / "search")
            with gzip.open(root / "search" / "sequence-timeline.json.gz", "rt", encoding="utf-8") as file:
                timeline = json.load(file)
            self.assertEqual(timeline, {
                "schema": 1, "total": 5, "dated": 3,
                "months": [["2025-02", 2], ["2025-04", 1]],
            })
            connection.close()

    def test_match_flags_and_sequence_lookup_use_full_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = connect_catalog(root / "catalog.sqlite")
            sequence = "ACGT" * 30
            records = [
                {"id": 1, "taxon": {"id": 10, "name": "Genus", "rank": "genus"},
                 "ofvs": [{"field_id": 2330, "value": sequence}]},
                {"id": 2, "taxon": {"id": 11, "name": "Genus species", "rank": "species"},
                 "quality_grade": "research",
                 "ofvs": [{"field_id": 2330, "value": sequence}]},
                {"id": 3, "taxon": {"id": 12, "name": "Other species", "rank": "species"},
                 "ofvs": [{"field_id": 2330, "value": sequence}]},
                {"id": 4, "taxon": {"id": 11, "name": "Genus species", "rank": "species"},
                 "ofvs": [{"field_id": 2330, "value": "T" * 120}]},
            ]
            connection.executemany(
                "INSERT INTO observations (id,payload) VALUES (?,?)",
                [(record["id"], json.dumps(record)) for record in records],
            )
            self.assertEqual(build_search_index(connection, root / "search"), 4)
            with gzip.open(root / "search" / "index.json.gz", "rt", encoding="utf-8") as file:
                index = json.load(file)
            self.assertEqual(index["groupCounts"], [3, 1])
            self.assertEqual([row[10] & 2 for row in index["rows"]], [2, 2, 2, 0])
            with gzip.open(root / "search" / "details" / "0.json.gz", "rt", encoding="utf-8") as file:
                self.assertEqual(json.load(file), records)
            digest = hashlib.sha256(sequence.encode()).hexdigest()
            with gzip.open(root / "search" / "hashes" / f"{digest[:2]}.json.gz", "rt", encoding="utf-8") as file:
                self.assertEqual(json.load(file)[digest], [[0, 1]])
            connection.close()


if __name__ == "__main__":
    unittest.main()
