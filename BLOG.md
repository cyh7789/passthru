# Why I put PA agents on FalkorDB

Prior auth eats time. The AMA's 2025 PA survey still lands around 40 requests per physician per week and about 13 hours of staff work to chase them. In India the SLA is tighter: cashless pre-auth in 1 hour, discharge in 3. The failure mode is the same in both places — the packet is incomplete, it gets denied or bounced, someone refiles, and the clock starts over.

I built **passthru** for Graph Hacks 2026: one FalkorDB instance, a graph per payer, and agents that walk the graph to find what's missing *before* a deny. No LLM in the decision path. Rules are small executable expressions. Denies only land after a blind human review. I'm borrowing shapes that already work elsewhere — Taiwan NHI–style machine-readable rules plus a completeness precheck, an auto-approve list — and pointing them at synthetic US PA and India cashless cases.

## Three graphs, one box

- `tenant_us_acme` and `tenant_in_bharat`: payer graphs, same schema, separate data (rules, docs, procedures, cases).
- `provider_demo`: patients, coverage order, documents.

Payer graphs only hold a patient token, not a name. Agents bind to one tenant; anything else is refused and logged. Claim a case with a version CAS (`status + version` match or you get 0 rows). Twenty workers race the queue on purpose so most claims lose and move on — on a fresh load: 200 cases in under a second, ~440 CAS conflicts, zero lost cases, zero double-decides.

## Hero: same MRI, two payers

One synthetic lumbar MRI package goes to both graphs. Acme wants clinic note, neuro exam, and a PT record with ≥6 weeks — 3 weeks fails, the agent proposes a deny, a blind reviewer signs `MISSING_PT_RECORD`. Bharat wants clinic note, estimate, and ID — all there, approve.

That blind deny is the only path into a learned procedure. `proc-MRI-LSPINE` v2 CAS-swaps over v1 (SUPERSEDES / FIXES / LEARNED_FROM). The next Acme MRI still ships the stale 3-week package, but the clinic chart now has 7 weeks. v2 chases the current PT record from the provider graph first, then the rules approve. If the chart is still short, it pends with a chase note instead of going out to get denied.

## Does the memory help?

`make evalmem` (seed 42), 80 synthetic Acme MRI, split 40/40:

| | first-pass approve | could pass (chart ≥6 wks) |
|---|---|---|
| v1, batch A | 20/40 | 29 |
| v2, batch B | 32/40 | 32 |

Fair read: v1 got 20 of 29 possible; v2 got 32 of 32. v2 also stops parking the short ones for a human and pends them instead. Synthetic, dev-time only.

Cross-tenant isolation: 50 deliberate wrong-tenant MCP calls → 50 refused, 50 logged, other graph untouched. Decision replay: after a fresh swarm, Acme 10/10 and Bharat 10/10 paths re-walk clean against the live graph.

## The dashboard

`make dash` puts it all on one page (Starlette + plain HTML, graphs drawn with the MIT `@falkordb/canvas`): SLA tiles and the CAS ticker per payer, the cross-tenant probe, the blind reviewer worklist with each case's rule → criterion → doc path, procedure v1 → v2 as a small graph diff with the eval + replay numbers, and the "what to fix first" cluster map. Screenshots are in the [README](README.md#the-dashboard).

![payer ops](docs/dash-ops.png)

## Why FalkorDB is load-bearing

Pull it out and nothing here runs.

- **Multigraph** — real tenant walls, not a `WHERE tenant=` filter.
- **CAS claims** — versioned `MATCH … SET … RETURN` so 20 workers don't double-decide.
- **`algo.SPpaths`** — missing-doc chase is a shortest path Case → DocRequirement on the rule graph, not a hand-rolled walk.
- **`algo.labelPropagation`** — GAP edges from stuck cases → "what to fix first" clusters on the dash.

## Honest limits

All data is synthetic. Nothing here is a clinical or regulatory result. Decisions are rules only — no LLM yet. Agents never deny on their own; missing docs pend with a list. Demo video and the hackathon submit form are still outside this repo.

Primary track: **Agent Memory & Coordination**. Also entered under **Agents That Act on Connected Data**.

Repo: https://github.com/cyh7789/passthru — `make demo` is the one-shot walkthrough.
