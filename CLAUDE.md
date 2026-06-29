## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## reporting_frequency

Added reporting frequency tracking in `data/processed/supplier_map.duckdb`:
- Table: `reporting_frequency` with 100 active Australian suppliers
- Weekly (6): PORTA-TIMBER, QEP, KINCROME, AHE-SEPARATED-HALF-DAY, NULON, WILSON&BRADLEY
- Fortnightly (4): TIMEPET, SCANDIA, STROL, WHITES
- Monthly: Default for all others (ACOL, GALINTEL, DINDAS, etc.)
- On-request: PERMA, DECOR8, DATS

Test run commands:
- `max_run`: All 100 suppliers
- `mid_run`: Weekly + Fortnightly suppliers only (10 total)
- Regular: Single supplier testing
