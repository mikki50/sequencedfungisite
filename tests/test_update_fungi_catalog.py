import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.update_fungi_catalog import (
    compact_observation,
    connect_catalog,
    normalize_sequence,
    write_catalog,
)


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
            "ofvs": [{"field_id": 2330, "value": sequence}],
            "description": "not retained",
            "location": "not retained",
        })

        self.assertIsNotNone(compact)
        assert compact is not None
        self.assertEqual(compact["id"], 12)
        self.assertEqual(compact["photos"][0]["medium_url"], "https://example.org/medium.jpg")
        self.assertEqual(compact["ofvs"][0]["value"], sequence)
        self.assertNotIn("description", compact)
        self.assertNotIn("location", compact)

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


if __name__ == "__main__":
    unittest.main()
