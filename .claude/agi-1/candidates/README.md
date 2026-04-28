# candidates/ — filesystem-based history for agi-learn and agi-council

Each `iter_NNNN/` directory is a full snapshot of one learning or council iteration.
Future iterations grep across all prior candidates to ground their reasoning.

## Per-iteration files

| File | Written by | Purpose |
|---|---|---|
| `reasoning.md` | agi-learn, agi-council | what triggered this iter, why each mutation was proposed/applied/rejected |
| `score.json` | agi-learn, agi-1 (Phase 5) | `{"before": {...}, "after": {...}}` |
| `trace.log` | agi-learn | append-only step log |
| `insights-applied.json` | agi-learn | applied insights with `trace_evidence` |
| `council-synthesis.json` | agi-council | full synthesis with `prior_ref` and `counterfactual` |
| `harness-snapshot.txt` | agi-learn | sha256sum of files touched |
| `REGRESSION.md` | agi-learn | only when an iter regressed |

## Hard rules
1. Never delete an iter_NNNN. Reverted iterations are training data.
2. Never rewrite in place. New iter for the fix.
3. INDEX.json is append-only.
4. Cap prior-candidate reading at 2M tokens.
