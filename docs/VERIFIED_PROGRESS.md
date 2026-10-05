# Verified Progress Contract

Pre-Active distinguishes work attempts and evidence from trusted task progress.

    ATTEMPT != EVIDENCE != VERIFIED PROGRESS != COMPLETION
    MODEL CLAIM != VERIFIED PROGRESS

The durable progress ledger uses three states:

    CANDIDATE -> VERIFIED
              -> REJECTED

A checkpoint begins as CANDIDATE. Its summary, evidence object, producer, run, and run step are immutable evidence about what somebody or something claims was achieved.

Only an explicit verifier decision may transition a candidate to VERIFIED or REJECTED.

Terminal decisions are immutable. If new evidence later changes the assessment, create a new candidate rather than rewriting history.

## Why this exists

Pre-Active already treats external effects carefully:

    REQUEST != AUTHORITY != ATTEMPT != EFFECT != VERIFIED EFFECT

Long-running autonomous work needs the analogous boundary for task progress. A successful model turn, a tool response, a written file, or an executor's confidence may all be useful evidence. None of them automatically proves that the task actually advanced according to its acceptance criteria.

Verified progress gives later runtime policies a trustworthy signal for:

- completion gates;
- recovery from failed rounds;
- no-progress circuit breakers;
- checkpoint-based context reconstruction;
- version-safe continuation;
- optional experience/lesson compilation.

Those policies are consumers of the ledger. They must not weaken its verification boundary.

## Producer and verifier

A producer may be a model/executor, tool adapter, observer, operator, or deterministic checker. The producer is not thereby the verifier.

A verifier may be a deterministic test or acceptance harness, an external observer checking the real target, a human/operator, or a separately admitted review component.

A second model call is not automatically independent verification merely because it is a different role or context. The verifier identity and reason are recorded so consumers can decide how much authority that verification deserves.

## CLI

Record candidate evidence:

    pre-active --state .pre-active/state.db checkpoint add <run-id> \
      "Build artifact appears complete." \
      --producer executor \
      --evidence-json '{"artifact":"dist/app.exe","test_run":"123"}'

List all checkpoints:

    pre-active --state .pre-active/state.db checkpoint list <run-id>

List trusted progress only:

    pre-active --state .pre-active/state.db checkpoint list <run-id> --trusted

Verify:

    pre-active --state .pre-active/state.db checkpoint verify <checkpoint-id> \
      --verifier acceptance-harness \
      --reason "Independent acceptance checks passed."

Reject:

    pre-active --state .pre-active/state.db checkpoint reject <checkpoint-id> \
      --verifier auditor \
      --reason "Observed target still violates the acceptance criterion."

## Current scope

The ledger is intentionally not yet an automatic completion gate.

Pre-Active does not currently:

- force every run to produce checkpoints;
- let the model self-promote checkpoints to VERIFIED;
- automatically fail a run for lack of verified progress;
- automatically inject verified checkpoints into model context;
- treat VERIFIED as universal proof independent of verifier quality.

Those are later policy layers.

The next justified consumer is run-contract/version affinity, followed by a no-progress circuit breaker based on real verified progress rather than transcript churn or model confidence.
