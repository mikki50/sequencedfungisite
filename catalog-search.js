"use strict";

class CatalogSearch {
  constructor(data, baseUrl) {
    if (data.schema !== 1 || !Array.isArray(data.rows) ||
        !Array.isArray(data.groupCounts) || !Number.isInteger(data.detailSize) ||
        data.detailSize < 1) {
      throw new Error("The published search index is invalid.");
    }
    this.data = data;
    this.baseUrl = baseUrl;
    this.details = new Map();
    this.hashes = new Map();
    this.groups = Array.from({ length: data.groupCounts.length }, () => []);
    this.observationGroups = new Map();
    data.rows.forEach((row, position) => {
      if (!this.groups[row[9]]) throw new Error("The search index contains an invalid sequence group.");
      this.groups[row[9]].push(position);
      this.observationGroups.set(String(row[0]), row[9]);
    });
  }

  groupForObservation(id) {
    const group = this.observationGroups.get(String(id));
    if (group === undefined) throw new Error("Observation is missing from the search index.");
    return group;
  }

  static async getGzip(url) {
    if (typeof DecompressionStream === "undefined") {
      throw new Error("This browser does not support gzip catalog downloads.");
    }
    const response = await fetch(url);
    if (!response.ok || !response.body) {
      const reloadHint = response.status === 404 ? " The catalog may have been updated; reload the page." : "";
      throw new Error("Catalog request failed: " + response.status + "." + reloadHint);
    }
    return new Response(response.body.pipeThrough(new DecompressionStream("gzip"))).json();
  }

  static async open(url) {
    const data = await CatalogSearch.getGzip(url);
    return new CatalogSearch(data, new URL(".", url));
  }

  async loadCached(cache, id, path) {
    if (!cache.has(id)) cache.set(id, CatalogSearch.getGzip(new URL(path, this.baseUrl)));
    try {
      return await cache.get(id);
    } catch (error) {
      cache.delete(id);
      throw error;
    }
  }

  async getDetails(positions) {
    const size = this.data.detailSize;
    await Promise.all([...new Set(positions.map(position => Math.floor(position / size)))]
      .map(id => this.loadCached(this.details, id, `details/${id}.json.gz`)));
    return Promise.all(positions.map(async position => {
      const records = await this.details.get(Math.floor(position / size));
      const record = records[position % size];
      if (!record || record.id !== this.data.rows[position][0]) {
        throw new Error("Observation data does not match the search index. Reload the page.");
      }
      return record;
    }));
  }

  async findSequence(sequence) {
    if (!sequence) return null;
    if (!window.crypto?.subtle) throw new Error("This browser cannot search DNA sequences securely.");
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(sequence));
    const digest = [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, "0")).join("");
    const prefix = digest.slice(0, 2);
    const entries = (await this.loadCached(this.hashes, prefix, `hashes/${prefix}.json.gz`))[digest] || [];
    for (const [group, id] of entries) {
      const position = this.groups[group]?.find(index => this.data.rows[index][0] === id);
      if (position === undefined) throw new Error("The sequence index is invalid.");
      const [record] = await this.getDetails([position]);
      if (getObservationDNASequence(record) === sequence) return group;
    }
    return -1;
  }

  rowObservation(row) {
    const taxon = this.data.taxa[row[1]];
    return {
      id: row[0],
      taxon: { id: taxon[0], name: taxon[1], rank: row[10] & 1 ? "species" : "genus" },
      species_guess: taxon[3]
    };
  }

  async search(options) {
    const data = this.data;
    const sequenceGroup = options.globalGroup !== null
      ? options.globalGroup : await this.findSequence(options.sequence);
    const ids = new Set(options.ids);
    const positions = [];
    data.rows.forEach((row, position) => {
      if (options.globalGroup !== null) {
        if (row[9] === sequenceGroup) positions.push(position);
        return;
      }
      const taxon = data.taxa[row[1]];
      const user = data.users[row[4]];
      const matchesTaxon = options.taxonId !== null
        ? String(taxon[0]) === String(options.taxonId) ||
          taxon[2].some(id => String(id) === String(options.taxonId))
        : (taxon[1] || "").toLowerCase().includes(options.taxon);
      const matchesPlace = options.placeId !== null
        ? data.places[row[2]].some(id => String(id) === String(options.placeId))
        : !options.location || data.locations[row[3]].toLowerCase().includes(options.location);
      const matchesUser = options.observerId !== null
        ? String(user[0]) === String(options.observerId)
        : !options.observer || (user[1] || "").toLowerCase().includes(options.observer);
      const matchesProject = !options.project ||
        (options.projectId !== null &&
         data.projects[row[5]].some(id => String(id) === String(options.projectId)));
      const review = options.review;
      const matchesReview = !review || (review === "species-required" && !(row[10] & 1)) ||
        (review === "mismatched-identifications" && Boolean(row[10] & 2)) ||
        (review === "not-research-grade" && Boolean(row[10] & 4));
      if (matchesTaxon && (!ids.size || ids.has(String(row[0]))) &&
          matchesPlace && matchesUser && matchesProject &&
          (!options.after || (row[6] && row[6] >= options.after)) &&
          (!options.before || (row[6] && row[6] <= options.before)) &&
          (sequenceGroup === null || row[9] === sequenceGroup) &&
          (!options.threshold || data.groupCounts[row[9]] - 1 > options.threshold) &&
          matchesReview) positions.push(position);
    });
    const name = position => {
      const taxon = data.taxa[data.rows[position][1]];
      return taxon[1] || taxon[3] || "Unknown fungus";
    };
    const sortColumns = { observed_on: 6, created_at: 7, updated_at: 8 };
    if (options.sort === "name") {
      const collator = new Intl.Collator(undefined, { sensitivity: "base" });
      positions.sort((a, b) => collator.compare(name(a), name(b)));
    } else {
      const column = sortColumns[options.sort];
      if (column === undefined) throw new Error("Invalid sort order.");
      positions.sort((a, b) => data.rows[b][column].localeCompare(data.rows[a][column]));
    }
    const selected = positions.slice((options.page - 1) * options.perPage, options.page * options.perPage);
    const observations = await this.getDetails(selected);
    const summaries = new Map();
    selected.forEach((position, index) => {
      const row = data.rows[position];
      const sequence = getObservationDNASequence(observations[index]);
      summaries.set(sequence, {
        count: data.groupCounts[row[9]],
        observations: this.groups[row[9]].map(index => this.rowObservation(data.rows[index]))
      });
    });
    return { count: positions.length, observations, summaries };
  }
}
