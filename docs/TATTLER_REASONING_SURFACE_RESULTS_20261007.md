# Tattler reasoning-surface results — 2026-10-07

Status: OBSERVATION NOTE / MODEL-TARGET BOUNDARY

## Shared experiment result

On 2026-10-07, the same repository stress-test prompt was run through three ChatGPT surfaces while WorkLaptop was instrumented with Tattler plus a companion Codex process/network tracer.

Observed controlled windows:

- Desktop Chat, GPT-5.6 Sol High: **0 MXC launches** and **2 new established Codex TLS connections** in the companion tracer.
- ChatGPT Desktop Work, Ultra: **59 MXC launches** and **73 new established Codex TLS connections** using the same companion-tracer definitions.
- Firefox cloud Work, Max: browser-side traffic was observable locally, but the provider's server-side worker topology was not.

The bounded conclusion is that Desktop Work used materially different local orchestration from ordinary High Chat in this runtime. It does **not** establish that sockets or MXC processes equal agents, that connection fanout grants a reasoning tier, or that a client can promote High into Ultra/Max by imitating transport behavior.

Canonical detailed evidence is being preserved in `thebrazenbeard/tattler` PR #7 and the reasoning interpretation in `thebrazenbeard/rezon` PR #103.


## Why Pre-Active needs this result

Pre-Active has a provider-neutral model interface and durable named model targets. The Tattler experiment reinforces that the active cognitive target must be represented by explicit provider/model/surface configuration and receipt evidence, not inferred from runtime transport or child-process behavior.

```text
configured model target != observed socket pattern
autonomous turn != reasoning tier
process fanout != cognition count
connection fanout != provider entitlement
```

If Pre-Active later supports multiple reasoning classes or high-cost specialist calls, those should be explicit target descriptors and resource decisions. A scheduler should never decide that a stronger reasoning surface is present merely because Tattler reports more connections or child processes.

## Receipt implication

For model-backed turns, useful provenance may include caller/provider-declared product surface and reasoning label when actually exposed. Tattler observations can be attached separately as diagnostic runtime evidence. The two evidence classes must not be collapsed.

This experiment does not require a Pre-Active runtime change by itself.
