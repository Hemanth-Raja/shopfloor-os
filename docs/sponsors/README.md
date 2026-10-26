# Sponsor technology

Part of the [Shop-Floor OS README](../../README.md#sponsor-technology). One page per sponsor, each with the code it lives
in, the command that shows it working and the result we got.

- **[Zetaris](ZETARIS.md): the data layer.** Four Postgres sources (`sf_fleet`, `sf_hr`, `sf_plant`, `sf_ops`)
  federated by Zetaris and modelled once as the Unified Semantic Layer `floor`, with 9 entities, 9 primary and 13
  foreign keys. The page follows Zetaris' evaluation criteria: the use case, three exploratory queries per source
  with real results ([`zetaris/queries/`](../../zetaris/queries)), the USL DDL and queries that show it is active, a
  data-quality rule, time saved compared with hand-coding, and a product requirement drawn from friction in this
  build.
- **[Meterless](METERLESS.md): engines and the model gateway.** The World Model, H-MEM and Markovian engines,
  ported to Python, each running a feature and passing its conformance tests (39 passed). Nightshift is the only
  model provider, with one pinned app per model and Ed25519-signed receipts (6,215 of 6,215 verified).
- **[NVIDIA](NVIDIA.md): the models.** Which Nemotron model serves which agent role and why, the reasoning-budget
  choices, calls, tokens and latency per role from 3,603 logged calls, and Nemotron alone vs the full loop
  (6 of 27 first plans unsafe, 0 executed).
- **[Cursor](CURSOR.md): the build.** The project rules every coding tool follows, and the Nightshift client with
  its tests, with an honest note on what Claude Code wrote.
