# passthru

Multi-tenant prior authorization agents on FalkorDB. Built for Graph Hacks 2026 (WeMakeDevs x FalkorDB). Work in progress.

The idea: each payer gets its own graph of rules, required docs and learned procedures. Agents walk that graph to figure out what's missing before a request gets denied. I'm borrowing a few things that already work elsewhere (Taiwan NHI style machine-readable rules + an AI completeness precheck, an auto-approve list) and pointing them at US PA and India cashless pre-auth.

All data here is synthetic. Nothing is a real policy or a real patient.

## run it

```
make setup
make up        # falkordb on :6379, browser on http://localhost:3000
make data      # regenerates data/*.json (seed 42, already committed)
make load
make run       # single worker, decides every queued case
make hero      # hero case: same MRI to both payers, then procedure v2 (run after make load)
make test
```

## graphs

One FalkorDB instance, three graphs:

- `tenant_us_acme`, `tenant_in_bharat`: payer graphs, same schema, separate data
- `provider_demo`: patients, coverage, documents

Payer graph:

```
(:Payer)<-[:REQUIRES_PA]-(:Service)-[:AUTO_APPROVE_LIST]->(:Payer)
(:Rule)-[:APPLIES_TO]->(:Service)
(:Rule)-[:HAS_CRITERION]->(:Criterion)-[:NEEDS_DOC]->(:DocRequirement)
(:Procedure {id, version, active, success_rate})-[:FOR_SERVICE]->(:Service)
(:Procedure)-[:STEP {order, doc, field, at_least}]->(:Action)
(:Procedure)-[:SUPERSEDES]->(:Procedure), -[:FIXES]->(:DenialReason), -[:LEARNED_FROM]->(:Case)
(:Case {status, version, claimed_by, patient_token, deadline})-[:FOR_SERVICE]->(:Service)
(:Case)-[:HAS_EVIDENCE]->(:Evidence {type, data})-[:SATISFIES]->(:DocRequirement)
(:Case)-[:DECIDED {by, at, path_json}]->(:Decision {outcome, reason})
(:Reviewer {pseudonym})-[:REVIEWED {blind:true}]->(:Case)
(:DenialReason {code})
```

Provider graph:

```
(:Patient)-[:HAS_COVERAGE {order}]->(:Plan)-[:OF]->(:PayerRef {tenant})
(:Patient)-[:HAS_DOC]->(:Document {type, date})
```

Payer graphs only hold a patient token, no names. Agents are bound to one tenant and `tenant_graph()` refuses any other one.

## queries I lean on

What a case needs and what it actually sent (the multi-hop):

```cypher
MATCH (c:Case {id:$id})-[:FOR_SERVICE]->(s:Service)<-[:APPLIES_TO]-(r:Rule)
      -[:HAS_CRITERION]->(cr:Criterion)-[:NEEDS_DOC]->(d:DocRequirement)
OPTIONAL MATCH (c)-[:HAS_EVIDENCE]->(e:Evidence)-[:SATISFIES]->(d)
RETURN r.id, cr.id, cr.expr, d.type, e.data
```

Criteria are small expressions over the submitted docs, like `pt_record.weeks >= 6` or `estimate.amount <= 40000 and id_proof.verified`. `passthru/expr.py` evaluates them with a whitelist (names, fields, comparisons, and/or/not), no `eval`. A missing doc means pend, a failed criterion means a proposed deny that goes to a human, and a field the evidence doesn't have means review too.

Claim a case without two agents grabbing it (version CAS, 0 rows = someone else won):

```cypher
MATCH (c:Case {id:$id, status:'queued', version:$v})
SET c.status='claimed', c.claimed_by=$agent, c.version=$v+1
RETURN c.id
```

Who pays first (COB):

```cypher
MATCH (p:Patient {id:$id})-[h:HAS_COVERAGE]->(:Plan)-[:OF]->(r:PayerRef)
RETURN r.tenant ORDER BY h.order
```

`passthru/cob.py` walks that order. Primary always goes through. Secondary (the demo Medigap) only opens after a `primary_eob` document is on the patient — paid or denied both count. About half of the dual-coverage patients get one seeded so both paths show up. The flow prepends the COB steps to `path_json`.

Each case gets a deadline from its payer node when it lands (Acme 7d standard / 72h urgent, Bharat 1h cashless / 3h discharge). Workers pull by earliest deadline, and claims older than 60s go back in the queue with a bumped version so a dead worker's late write can't land.

A sweep flags cases in the last 25% of their window as at risk (they jump the queue), then handles overdue ones the way the payer says: Acme just marks a breach, Bharat auto-approves (demo rule in the spirit of PM-JAY deemed approval, not a legal claim). Workers only write a decision if they still hold the claim, so a late worker can't double-decide.

Every decision stores the rule -> criterion -> doc path it used in `path_json`, so you can rerun it in the browser.

Proposed denies and no-rule / low-confidence cases land as `needs_review` with no Decision yet. `passthru/review.py` lists them blind (service, reason, path — no patient name or facility), lets a reviewer claim one, and `submit_review` is the only path that can write a deny.

## status

Done: compose, synthetic data (300 patients, 200 requests), loader, single-worker rules-only flow, deadline queue + stale claim reclaim, at-risk / overdue sweep, criteria evaluated against evidence, COB routing (coverage order + primary EOB gate), blind human review queue, procedure versioning (reviewer deny -> v2, CAS swap, chase step), hero case end to end, tests.
Next: 20 workers, MCP tools, dashboard.

No LLM yet, decisions are rules only. Missing docs = pend with a list, never an auto deny. Agents never deny on their own.

## AI note

I used an AI coding assistant for implementation. I designed the graph model and the agent roles.

## hero case

`make hero` sends one synthetic lumbar MRI package (clinic note, neuro exam, 3 weeks of PT, estimate, ID) to both payers. Same request, two graphs, two doc lists:

- Acme wants `clinic_note`, `neuro_exam`, `pt_record`. 3 weeks fails `pt_record.weeks >= 6`, the agent proposes a deny, a blind reviewer signs it off as `MISSING_PT_RECORD`.
- Bharat wants `clinic_note`, `estimate`, `id_proof`. All there, approved by the agent.

Then part 2:

- The reviewer's fix (`chase_doc pt_record.weeks >= 6`) goes through `procedure.learn_from_review` and becomes `proc-MRI-LSPINE` v2. It only works off a blind reviewer's deny, and the swap is a version CAS (v1 has to still be active at version 1, or 0 rows and you lost).
- Next Acme MRI ships the same stale package (3 weeks PT), but the clinic chart now has 7. v2 runs the chase step first, pulls the current PT record from the provider graph, swaps it onto the case (keeps the original as `prev_data`), and the rules approve it.
- If the chart is still short, it pends with "chase: pt_record.weeks 4 < 6" instead of going out to get denied.
- Success rate per version: v1 0/1, v2 1/1.
