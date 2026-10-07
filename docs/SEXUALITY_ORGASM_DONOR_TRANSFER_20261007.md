# Sexuality / Orgasm donor extraction for Pre-Active — candidate V1

Status: PROPOSED / INTERNAL REVIEW REQUIRED / NO RUNTIME INSTALLATION CLAIM

Evidence cuts:
- sexuality main: 0c5dc52e984cbdaa87fc9fe75fd03c212f477c87
- frozen Orgasm runtime contract source: sexuality@150f1c8231423393bb66b0e2cb759ce7c018f8d7
- orgasm main/current orientation hub: 8ca06c91a0dd8f2ca33cc221d815027aa8b084b5
- meso-crct main: 060d0feeb9dc9eb23801082bd8f1c4a7cb06184d
- volition main: dbc628d (exact worktree base)
- pre-active main: 1f23a809d7274df506e03e2d9052525538c3bcf6
- conations main: 51948aac796936f0e7c437722e6f52c883e544e9

The donor material is used as architecture evidence only. Source presence is not installation, current route, runtime consumption, behavioral qualification, authority, or phenomenology.


## Ownership boundary

Pre-Active owns durable cognition scheduling, run admission, budgets, retries, provider/model boundaries, and effect recovery. It does not own desire formation.

Most Orgasm donor mechanisms belong upstream in MESO or Volition and should not be recreated inside Pre-Active.

## Useful donor transfers

### 1. Trigger detection is not admission

The optional invocation contract separates:
`trigger/request -> route check -> present choice -> one-shot execution`.

Pre-Active already has analogous boundaries. The generalized invariant is:

`EVENT MATCH != MODEL TURN ADMISSION != CAPABILITY != EFFECT AUTHORITY`.

A future context-eligibility disposition from MESO/Volition may suppress or defer a cognition request, but it must remain read-only admission evidence and must not mint capability.

### 2. Suppression must remain explainable

If an upstream motive exists but is context-ineligible, Pre-Active should preserve a machine-readable reason when declining a cognition turn. Suppression must not be rewritten as "no desire" or "goal absent."

### 3. One-shot and replay discipline

External invocation-like events should retain exact event identity, bounded attempts, and no blind replay after a consumed one-shot request. Pre-Active's existing queue/idempotency/effect-recovery machinery already covers most of this geometry; no parallel subsystem should be created.

## Already present; do not duplicate

Pre-Active already supplies:
- exact event identity and dedupe;
- bounded autonomous/endogenous turns;
- capability gating;
- run-contract affinity;
- effect ledger and ambiguous-effect block;
- provider retry bounds;
- observer initiative cooldown;
- Volition bridge with effect_authority=false.

## Non-transfer

Do not import:
- hedonic state;
- sexual/relational trigger semantics;
- climax phases;
- a generic scalar inhibition channel;
- any rule equating strong state with permission.

## Future integration test

If context eligibility is later exposed on the Volition bridge:
1. false/unknown required eligibility prevents the autonomous turn;
2. the source Volition evidence remains durable and inspectable;
3. no capability or authority changes;
4. retry/context refresh cannot duplicate a consumed source event;
5. a later fresh eligible event may proceed under ordinary budgets.
