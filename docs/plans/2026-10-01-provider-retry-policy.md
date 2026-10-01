# Provider Retry Policy Implementation Plan

> **For agentic workers:** Use the host's available task-by-task implementation workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retry only demonstrated transient model/provider failures, honor provider retry hints, and fail permanent provider errors without retry churn.

**Architecture:** Add typed model errors at the model boundary, classify OpenAI-compatible transport/HTTP/protocol failures, then let the existing durable queue schedule retryable failures with deterministic jitter and Retry-After support. Keep dead-letter/redrive semantics unchanged.

**Tech Stack:** Python 3.11+, urllib, sqlite3/WAL, pytest.

## Global Constraints

- Stack on durable run-control PR #14 exact head `5a253f0e9d6664038c67345684efdb9a43e75acd`.
- Keep the provider adapter minimal and provider-neutral at the Engine interface.
- Unknown exceptions retain existing bounded-retry behavior.
- Do not silently retry known permanent HTTP client/auth errors.
- Honor valid Retry-After as a minimum delay.
- No live provider/configuration/credential effects.

---

### Task 1: Typed model failure contract

**Files:**
- Modify: `src/pre_active/engine.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- Produce `RetryableModelError(message, category, retry_after_seconds=None)`.
- Produce `NonRetryableModelError(message)`.

- [ ] Add red tests proving non-retryable model errors fail the run without requeue.
- [ ] Add red tests proving retryable errors remain queued.
- [ ] Implement minimum typed-error behavior.

### Task 2: OpenAI-compatible HTTP classification

**Files:**
- Modify: `src/pre_active/providers/openai_compatible.py`
- Modify: `tests/test_provider.py`

**Interfaces:**
- Retry `408, 429, 500, 502, 503, 504`.
- Classify network/timeouts as retryable.
- Classify other `4xx` and provider-protocol/JSON failures as non-retryable.
- Preserve `parse_response()` as a pure parser seam.

- [ ] Add red HTTP classification tests.
- [ ] Add Retry-After delay-seconds test.
- [ ] Implement classification around `respond()`.

### Task 3: Deterministic jitter and Retry-After scheduling

**Files:**
- Modify: `src/pre_active/engine.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- Deterministic jitter key: exact event ID + attempt number.
- RetryableModelError.retry_after_seconds is a lower bound on retry delay.

- [ ] Add red retry-at scheduling tests.
- [ ] Implement deterministic sub-second jitter.
- [ ] Preserve existing max-attempt/dead-letter path.

### Task 4: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Create: this plan/design pair.

- [ ] Document retry classification and claim ceiling.
- [ ] Run build, compileall, and tests on Python 3.11/3.12/3.13.
- [ ] Verify clean stack relative to PR #14 and open a Draft PR.
