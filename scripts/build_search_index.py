"""Build a compact browser search index and on-demand observation shards."""

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path


DETAIL_SIZE = 128


def species_level(taxon):
    rank = (taxon.get("rank") or "").lower()
    try:
        level = float(taxon.get("rank_level") or 0)
    except (ValueError, TypeError):
        level = 0
    return rank in ("species", "subspecies", "variety", "form") or 0 < level <= 10


def write_gzip(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as compressed:
            compressed.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def build_search_index(connection: sqlite3.Connection, output_dir: Path):
    tables = {name: {"values": [], "lookup": {}} for name in
              ("taxa", "places", "locations", "users", "projects")}

    def intern(name, value):
        table = tables[name]
        key = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if key not in table["lookup"]:
            table["lookup"][key] = len(table["values"])
            table["values"].append(value)
        return table["lookup"][key]

    groups = {}
    rows = []
    details = []
    hashes = [{} for _ in range(256)]
    for (payload,) in connection.execute("SELECT payload FROM observations ORDER BY id"):
        record = json.loads(payload)
        sequence = next(
            (field["value"] for field in record.get("ofvs", [])
             if str(field.get("field_id")) == "2330"),
            None,
        )
        if not sequence:
            raise ValueError(f"Observation {record['id']} has no ITS sequence")
        taxon = record.get("taxon") or {}
        user = record.get("user") or {}
        if sequence not in groups:
            groups[sequence] = []
            digest = hashlib.sha256(sequence.encode()).hexdigest()
            hashes[int(digest[:2], 16)].setdefault(digest, []).append(
                [len(groups) - 1, record["id"]]
            )
        group = groups[sequence]
        if not group:
            group_id = len(groups) - 1
        else:
            group_id = rows[group[0]][9]
        is_species = species_level(taxon)
        rows.append([
            record["id"],
            intern("taxa", [taxon.get("id"), taxon.get("name"),
                            taxon.get("ancestor_ids") or [], record.get("species_guess")]),
            intern("places", record.get("place_ids") or []),
            intern("locations", record.get("place_guess") or ""),
            intern("users", [user.get("id"), user.get("login")]),
            intern("projects", record.get("project_ids") or []),
            record.get("observed_on") or "", record.get("created_at") or "",
            record.get("updated_at") or "", group_id,
            (1 if is_species else 0) | (4 if record.get("quality_grade") != "research" else 0),
        ])
        group.append(len(rows) - 1)
        details.append(record)
        if len(details) == DETAIL_SIZE:
            write_gzip(output_dir / "details" / f"{(len(rows) - 1) // DETAIL_SIZE}.json.gz", details)
            details = []
    if details:
        write_gzip(output_dir / "details" / f"{(len(rows) - 1) // DETAIL_SIZE}.json.gz", details)

    counts = [0] * len(groups)
    for group in groups.values():
        counts[rows[group[0]][9]] = len(group)
        species = [rows[position] for position in group if rows[position][10] & 1]
        ids = {str(tables["taxa"]["values"][row[1]][0]) for row in species
               if tables["taxa"]["values"][row[1]][0]}
        names = {tables["taxa"]["values"][row[1]][1] for row in species}
        names_without_id = {tables["taxa"]["values"][row[1]][1] for row in species
                            if not tables["taxa"]["values"][row[1]][0]}
        for position in group:
            row = rows[position]
            taxon_id, name = tables["taxa"]["values"][row[1]][:2]
            if not row[10] & 1:
                conflict = bool(species)
            elif taxon_id:
                conflict = any(value != str(taxon_id) for value in ids) or any(
                    value != name for value in names_without_id
                )
            else:
                conflict = any(value != name for value in names)
            if conflict:
                row[10] |= 2
    for prefix, values in enumerate(hashes):
        write_gzip(output_dir / "hashes" / f"{prefix:02x}.json.gz", values)
    write_gzip(output_dir / "index.json.gz", {
        "schema": 1, "detailSize": DETAIL_SIZE, "rows": rows,
        "groupCounts": counts,
        **{name: table["values"] for name, table in tables.items()},
    })
    return len(rows)
