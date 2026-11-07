"""Runner implementation package (runtrack).

Modules:
  archive    daily archive + retention pruning
  ban        shop banning (never-sold-out detection)
  db         DB verification + snapshot helpers
  env        paths, constants, logging
  guard      heartbeat-based takeover guard
  heartbeat  status/last_ok.json read/write
  jobs       checkpoint / ranking / prune / publish
  logs       run_log bookkeeping
  scraper    subprocess launcher with log capture
  state      per-process mutable run context
  storage_ops  pull / upload / rollback safeguards
"""
