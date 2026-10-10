# Index benchmark

Timestamp: 2026-10-10T20:25:31.678638+00:00
Chunk rows: 5486

## Build time

| Index | Seconds |
| --- | ---: |
| HNSW | 10.924 |
| IVFFlat | 0.832 |

## Unfiltered recall

| Index | Setting | Recall@10 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: |
| HNSW ef_search | 40 | 0.998 | 2.67 | 10.65 |
| HNSW ef_search | 100 | 0.998 | 5.13 | 7.41 |
| HNSW ef_search | 250 | 1.000 | 8.48 | 13.66 |
| IVFFlat probes | 1 | 0.752 | 1.16 | 2.37 |
| IVFFlat probes | 5 | 0.905 | 2.72 | 4.75 |
| IVFFlat probes | 10 | 0.951 | 4.13 | 6.47 |

## Filtered HNSW recall (ef_search = 40, post-filter)

| Filter | Selectivity | Recall@10 |
| --- | ---: | ---: |
| category=cs.CL | 0.294 | 1.000 |
| category=cs.DB | 0.384 | 1.000 |
| category=cs.IR | 0.322 | 1.000 |
| year=2023 | 0.301 | 1.000 |
| year=2024 | 0.319 | 1.000 |
| year=2025 | 0.199 | 1.000 |
| year=2026 | 0.181 | 1.000 |
