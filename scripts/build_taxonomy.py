"""Build a taxonomic tree from sequenced observations in the catalog."""

import gzip
import json
import sys
import urllib.parse
import urllib.request

if __package__:
    from .build_search_index import write_gzip
else:
    from build_search_index import write_gzip


FUNGI_ID = 47170
TAXA_API = "https://api.inaturalist.org/v1/taxa"
TAXA_BATCH_SIZE = 200


def build_taxonomy(connection, output_dir, request_json, previous_url=None):
    taxa = {}
    if previous_url:
        with urllib.request.urlopen(previous_url, timeout=180) as response:
            with gzip.GzipFile(fileobj=response) as compressed:
                cached = json.load(compressed)
        if cached.get("schema") != 1 or not isinstance(cached.get("taxa"), list):
            raise ValueError("The previous taxonomy catalog is invalid.")
        taxa = {int(item["id"]): item for item in cached["taxa"] if item.get("name")}

    observations = []
    needed = {FUNGI_ID}
    for (payload,) in connection.execute("SELECT payload FROM observations"):
        record = json.loads(payload)
        taxon = record.get("taxon") or {}
        taxon_id = taxon.get("id")
        ancestry = taxon.get("ancestor_ids") or []
        if taxon_id:
            path = list(dict.fromkeys([int(value) for value in ancestry] + [int(taxon_id)]))
            observations.append(path)
            needed.update(path)
        else:
            observations.append([])

    requested = set()
    while True:
        missing = sorted(needed - taxa.keys() - requested)
        if not missing:
            break
        for offset in range(0, len(missing), TAXA_BATCH_SIZE):
            batch = missing[offset:offset + TAXA_BATCH_SIZE]
            params = urllib.parse.urlencode({"id": ",".join(map(str, batch)), "per_page": 200})
            data = request_json(TAXA_API + "?" + params, api_request=True)
            if not isinstance(data.get("results"), list):
                raise ValueError("Invalid response from the iNaturalist taxon API.")
            for item in data["results"]:
                taxon_id = int(item["id"])
                if taxon_id not in batch or not item.get("name"):
                    raise ValueError("Unexpected or invalid taxon in iNaturalist response.")
                ancestors = [int(value) for value in (item.get("ancestor_ids") or [])]
                taxa[taxon_id] = {
                    "id": taxon_id, "name": item["name"],
                    "rank": item.get("rank") or "unranked", "ancestor_ids": ancestors,
                }
                needed.update(ancestors)
            requested.update(batch)
            if len(requested) % 5000 < len(batch):
                print(f"Looked up {len(requested)} taxa.", flush=True)

    missing = needed - taxa.keys()
    if missing:
        print(f"Warning: {len(missing)} taxon names could not be retrieved.", file=sys.stderr)

    nodes = {}
    unplaced = 0
    for recorded_path in observations:
        if not recorded_path:
            unplaced += 1
            continue
        identified = recorded_path[-1]
        taxon = taxa.get(identified)
        path = (taxon["ancestor_ids"] + [identified]) if taxon else recorded_path
        if FUNGI_ID not in path:
            unplaced += 1
            continue
        path = list(dict.fromkeys(path[path.index(FUNGI_ID):]))
        for index, taxon_id in enumerate(path):
            metadata = taxa.get(taxon_id)
            parent = path[index - 1] if index else None
            node = nodes.get(taxon_id)
            if node is None:
                node = {
                    "id": taxon_id,
                    "name": metadata["name"] if metadata else f"Unknown taxon #{taxon_id}",
                    "rank": metadata["rank"] if metadata else "unknown",
                    "parent": parent,
                    "count": 0,
                    "direct": 0,
                }
                nodes[taxon_id] = node
            elif node["parent"] != parent:
                raise ValueError(f"Inconsistent ancestry for taxon {taxon_id}.")
            node["count"] += 1
        nodes[identified]["direct"] += 1

    write_gzip(output_dir / "taxonomy.json.gz", {
        "schema": 1,
        "total": len(observations),
        "unplaced": unplaced,
        "missing_taxa": len(missing),
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "taxa": sorted(taxa.values(), key=lambda item: item["id"]),
    })
