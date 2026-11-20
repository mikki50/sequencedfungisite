"use strict";

async function runCatalogSearchTests(Search) {
  const data = {
    schema: 1, detailSize: 128, groupCounts: [4],
    rows: [
      [1, 0, 0, 0, 0, 0, "", "", "", 0, 1],
      [2, 0, 1, 0, 0, 0, "", "", "", 0, 1],
      [3, 0, 2, 0, 0, 0, "", "", "", 0, 1],
      [4, 0, 3, 0, 0, 0, "", "", "", 0, 1]
    ],
    taxa: [[1, "Example", [], null]], places: [[1, 10], [10], [2, 10], []],
    locations: [""], users: [[1, "observer"]], projects: [[]]
  };
  const index = new Search(data, "https://example.org/");
  index.getDetails = async () => [];
  const options = {
    globalGroup: null, sequence: "", ids: [], taxon: "", taxonId: null,
    placeId: null, countryPlaceId: null, countryIds: null, location: "",
    observer: "", observerId: null, project: "", projectId: null, after: "",
    before: "", threshold: 0, review: "", sort: "name", page: 1, perPage: 0
  };
  const cases = [
    [{ placeId: 10, countryPlaceId: 1 }, 1],
    [{ countryIds: new Set([1, 2]) }, 2],
    [{ placeId: 10 }, 3],
    [{ placeId: 1 }, 1]
  ];
  for (const [filters, expected] of cases) {
    const result = await index.search({ ...options, ...filters });
    if (result.count !== expected) {
      throw new Error(`Expected ${expected} matching observations; got ${result.count}.`);
    }
  }
}

if (typeof module !== "undefined" && require.main === module) {
  const fs = require("node:fs");
  const path = require("node:path");
  const vm = require("node:vm");
  const source = fs.readFileSync(path.join(__dirname, "..", "catalog-search.js"), "utf8");
  const Search = vm.runInThisContext(source + "\nCatalogSearch;");
  runCatalogSearchTests(Search).then(
    () => console.log("Catalog search tests passed."),
    error => { console.error(error); process.exitCode = 1; }
  );
}
