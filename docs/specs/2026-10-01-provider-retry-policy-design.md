# Pre-Active Provider Retry Policy V1

Status: stacked implementation target on top of durable run-control PR #14.

## Problem

The OpenAI-compatible provider boundary currently lets transport, HTTP, authentication, throttling, server, JSON, and provider-protocol failures escape as ordinary exceptions. The engine then sends nearly all of them through the same bounded event retry path.

That means a permanent 401/403/404/422 or a structurally invalid provider response can consume the same retry budget as a timeout, 429, or 503.

## Research basis

Current provider guidance converges on narrower retry predicates:

- Google model retry guidance recommends retrying transient `408`, `429`, `500`, `502`, `503`, and `504`, using exponential backoff plus jitter, and not retrying other client `4xx` errors.
- RFC 9110 defines `Retry-After` as either delay-seconds or an HTTP date and describes it as the requested delay before a follow-up request, including for temporary `503` service unavailability.
- AWS SDK guidance uses error-type-specific retry behavior, exponential backoff with jitter, and bounded retry quotas so clients fail fast instead of amplifying outages.
- Temporal distinguishes infrastructure/task failures that may be retried from execution failures that should close rather than churn indefinitely.

Pre-Active should import those failure-semantics invariants, not a provider SDK.

## Model error types

The model boundary exposes two durable orchestration classes:

```text
RetryableModelError(category, retry_after_seconds?)
NonRetryableModelError
```

Retryable categories are descriptive evidence for scheduling, not authority.

The OpenAI-compatible adapter maps:

- network/connection/timeouts -> retryable;
- HTTP 408 -> retryable transient;
- HTTP 429 -> retryable throttling;
- HTTP 500/502/503/504 -> retryable transient;
- other HTTP 4xx -> non-retryable;
- provider JSON/protocol shape failure -> non-retryable;
- other HTTP codes -> fail closed as non-retryable unless explicitly admitted later.

## Retry timing

Existing bounded queue retries remain the retry mechanism. The engine computes:

1. the existing capped exponential delay;
2. deterministic per-event/per-attempt jitter in `[0, 1)` seconds so multiple events do not synchronize while tests remain reproducible;
3. if `Retry-After` is present, the scheduled delay is at least the provider-requested delay.

Retry exhaustion still uses the existing `DEAD` / redrive contract.

## Permanent provider failure

A `NonRetryableModelError` fails the run at the current fenced event boundary and consumes the event without scheduling a retry. The failure and classification are journaled.

This does not classify arbitrary Python/programming exceptions as permanent. Unknown exceptions continue through the existing bounded retry path.

## Hostile review

> **HOSTILE REVIEWER:** Treat every provider failure as retryable because model endpoints are flaky.

**REJECTED WITH EVIDENCE.** Retrying authentication, malformed-request, and other permanent client failures wastes capacity and can amplify incidents. Current Google/AWS guidance explicitly predicates retry on transient error classes.

> **HOSTILE REVIEWER:** Use random jitter exactly like cloud SDKs.

**PARTIALLY ACCEPTED.** Jitter is valuable, but runtime tests and replay should remain reproducible. Pre-Active derives a stable fractional jitter from event ID + attempt. Different events spread out; the same durable event schedules the same delay.

> **HOSTILE REVIEWER:** Add a circuit breaker and token-bucket retry quota now.

**REJECTED FOR THIS LANE.** They may be valuable later, but typed failure semantics are the prerequisite. This change stays small enough to audit.

> **HOSTILE REVIEWER:** Malformed model output might succeed on another sample.

**PARTIALLY ACCEPTED.** That is possible, but it is not a demonstrated transport/transient failure. V1 fails closed rather than spending the infrastructure retry budget on semantic/protocol repair. A separate model-repair policy can be added explicitly later.

## Claim ceiling

Source-level provider failure classification and queue scheduling only. No live provider configuration, credential mutation, or runtime cutover is included.
