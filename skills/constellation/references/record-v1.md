# Record contract

`capture` takes an key and record object. The record contains id,
projects, kind, state, evidence, claim, terms, links and sources. Each
source includes id, room, revision, locator, and an optional excerpt. Hashes
are derived from excerpts. Consult the validator in constellation_store.py
for exact field limits and required fields. Do not guess unknown provenance.
Feedback targets an exact record revision and reports observed usefulness;
repeated retrieval alone does not establish success.
