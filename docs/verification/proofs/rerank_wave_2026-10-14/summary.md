# Reranker + ONNX embedding + RRF weight wave — LoCoMo evidence (2026-10-14)

Tree: HEAD fdb83e5 + this wave's working-tree changes (not committed).
Harness: `python -m machina.research.benchmark --section locomo --locomo-db [--locomo-rerank]`
Data: `~/.machina/locomo/locomo10.json` (10 conversations, 1986 questions, 1977 with evidence).
DB: local PostgreSQL/pgvector. All runs sequential; logs in this directory.

## Configurations (env overrides; `.env` pins MACHINA_EMBEDDING_BACKEND=local by default)

| Log | Embedding | RRF weights (kw/dense) | Rerank | search_mode |
|---|---|---|---|---|
| A_kwonly_rerank_off.log | off | 1.0 / 1.0 | off | keyword-only x1982 |
| B_kwonly_rerank_on.log | off | 1.0 / 1.0 | local ONNX | keyword-only x1982 |
| C_bgesmall_rerank_off.log | onnx (bge-small-en-v1.5) | 1.0 / 1.0 | off | hybrid x1982 |
| D_onnx_w1_rerank_on.log | onnx | 1.0 / 1.0 | local ONNX | hybrid x1982 |
| E_potion_rerank_on.log | local (potion-base-8M) | 1.0 / 1.0 | local ONNX | hybrid x1982 |
| F1_fusion_only_prebug.log | onnx | 1.0 / 0.25 | off (pre-fix fusion only) | hybrid x1982 |
| F1b_onnx_dw0.25_rerank_on.log | onnx | 1.0 / 0.25 | local ONNX | hybrid x1982 |
| F2_fusion_only_prebug.log | onnx | 1.0 / 0.5 | off (pre-fix fusion only) | hybrid x1982 |

## Results (archive replay; headline = post-rerank when rerank ran)

| Config | recall@5 | hit@5 | MRR@5 | recall@10 | hit@10 | MRR@10 | rerank p50 / mean (ms) | rerank status |
|---|---|---|---|---|---|---|---|---|
| A keyword-only, rerank off | 0.4249 | 0.4608 | 0.3447 | 0.4926 | 0.5362 | 0.3550 | — | — |
| B keyword-only, rerank on | 0.4395 | 0.4709 | 0.3667 | — | — | — | 250.6 / 281.2 | applied 1976, skipped 6 |
| C bge-small w1.0, rerank off | 0.4147 | 0.4522 | 0.3122 | 0.4937 | 0.5387 | 0.3238 | — | — |
| D bge-small w1.0, rerank on | 0.4326 | 0.4679 | 0.3621 | — | — | — | 213.8 / 254.1 | applied 1982 |
| E potion w1.0, rerank on | 0.4302 | 0.4654 | 0.3643 | — | — | — | 228.1 / 260.2 | applied 1982 |
| F1 bge-small w0.25, rerank off | 0.4383 | 0.4780 | 0.3341 | 0.5158 | 0.5604 | 0.3450 | — | — |
| F1b bge-small w0.25, rerank on | 0.4484 | 0.4810 | 0.3733 | — | — | — | 191.5 / 206.3 | applied 1982 |
| F2 bge-small w0.5, rerank off | 0.4247 | 0.4618 | 0.3248 | 0.4967 | 0.5417 | 0.3357 | — | — |

Reference rows: TF-IDF baseline recall@5 0.440 / MRR 0.354; keyword-only component (all runs)
0.4249 / 0.4608 / 0.3447. Dense-alone component (bge-small): 0.3539 / 0.3895 / 0.2416.
Session recall unchanged everywhere (0.425 / 0.461 / 0.345, ~9–13 ms; no rerank seam there).

## Bar arithmetic (go/no-go: Δrecall@5 ≥ +0.02, ΔMRR ≥ +0.03, Δhit ≥ −0.005, rerank p50 ≤ +150 ms)

- Reranker default (B vs A): Δrecall +0.0146 ✗, ΔMRR +0.0220 ✗, Δhit +0.0101 ✓, p50 +250.6 ms ✗ → stays off.
  Cross-check D vs C: Δrecall +0.0179 ✗, ΔMRR +0.0499 ✓, Δhit +0.0157 ✓, p50 +213.8 ms ✗.
- Embedding default (best isolated, F1 vs A): Δrecall +0.0134 ✗, ΔMRR −0.0106 ✗ →
  bge-small does not beat keyword-only; dense stays available-but-not-default.
  Best composite F1b vs A: Δrecall +0.0235 ✓, ΔMRR +0.0286 ✗ (misses +0.03 by 0.0014), Δhit +0.0202 ✓.
- RRF dense weight (fused vs its keyword component): 1.0 → recall −0.0102 / MRR −0.0325;
  0.5 → −0.0002 / −0.0199; 0.25 → +0.0134 / −0.0106. Chosen: 0.25 (only value where the fused
  ranking leads its keyword component on recall; MRR drag minimized at −0.011). No tested weight
  makes hybrid MRR ≥ keyword-only MRR, so dense stays opt-in (as instructed).

## Cold start / latency context

- fastembed first call: ONNX embed 549.8 ms (warm cache); ONNX rerank 7391.6 ms (download ~120 MB + session + first inference).
- Warm rerank p50 191–251 ms for 10 candidates (MiniLM-L12, ORT CPU, in-process).
- Hybrid query latencies (DB retrieval only): 15–22 ms; session recall 9–13 ms.

## Provenance notes

- F1/F2 logs were produced before a harness fix (`_rerank_ids` assumed tuple candidates while the
  hybrid path passes `SearchResult`); their rerank phase was SKIPPED. Fusion numbers remain valid;
  F1b is the corrected 0.25 run with rerank.
- Pre-fix note kept in file names (`*_prebug`).
