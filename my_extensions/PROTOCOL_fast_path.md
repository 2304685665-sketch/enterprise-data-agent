# PROTOCOL: fast path (direct + schema hint + skip compress + skip brief)
Frozen 2026-10-13 BEFORE any run of D2/D3/D4. Suite main2 (63 cases), model openai:gpt-4.1-mini, scorer v1 (preregistered).

Arms (all flags default off; each arm = fresh out-dir):
- A0 = main2_final (baseline, already run)
- D1 = direct_main2 (ODR_DIRECT_RESEARCH=1, already running)
- D2 = D1 + ODR_SQL_SCHEMA_HINT=1
- D3 = D2 + ODR_SKIP_COMPRESS=1
- D4 = D3 + ODR_SKIP_BRIEF=1

Primary comparison: D4 vs A0, mean model calls per case (paired Wilcoxon). Secondary: cost, v1 pass count.
Quality guard: D4 v1 pass must not be >2 lower than A0, and McNemar exact p>0.05 for pass loss; otherwise D4 is reported as a
speed/quality trade-off, not an improvement. All four arms are reported whatever the outcome, including null/negative.
Known risk stated upfront: skipping compress/brief removes LLM normalisation, so final-report quality may drop; a
negative result will be reported as such. No case or scorer edits after seeing results.
