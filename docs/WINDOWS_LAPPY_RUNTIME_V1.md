# Windows Lappy Runtime V1

Status: host-specific runtime qualification record and reproducible operating profile.

Naming migration note: the behavioral observations in this record were gathered before the runtime was standardized under the Pre-Active name. Exact historical commit identities remain evidence, but predecessor literal task names, environment-variable names, probe strings, and filesystem paths are intentionally not reproduced here. The Pre-Active names and paths below describe the current source configuration and do not by themselves prove that the previously qualified host has been migrated or requalified.

This document records the first persistent Pre-Active deployment qualified on the Lappy Windows host. It is evidence about that exact host binding, not a claim that every Windows installation or model backend is qualified.

## Bound source and model

- Pre-Active source at initial activation: `thebrazenbeard/pre-active@0abf6045d8e195557cebe9a37b86f2a1eaa9bbbe`.
- Post-reboot installed source after PR #2 merged: `thebrazenbeard/pre-active@2ed253c1b78af1cc9bd163e346d20f2fd86348f5`.
- Runtime-package restack base/current repository `main`: `thebrazenbeard/pre-active@6234a29f8a0fa5fa772212146c697588e6dbbf56`.
- Model source: `rodrigomt/Qwen3.5-4B-Uncensored-Aggressive@d61dd146c8fd44c9a49cdb7f59f34e17b61902d8`.
- Model endpoint: loopback-only `http://127.0.0.1:18081/v1`.
- Advertised model ID: `qwen3.5-4b-local`.
- Runtime root: `C:\ProgramData\PreActive`.
- Persistent state: `C:\ProgramData\PreActive\state\state.db`.

The persistent host layout pins a Python 3.12 runtime and the model inference packages under the Pre-Active root instead of depending on a temporary workspace.

## Runtime topology

```text
Task Scheduler (SYSTEM)
  |
  +-- PreActive Qwen Endpoint
  |     -> start-qwen.ps1
  |     -> waits for >= configured free VRAM
  |     -> qwen_http.py
  |     -> 127.0.0.1:18081/v1
  |
  +-- PreActive Daemon
  |     -> start-pre-active.ps1
  |     -> waits for /v1/models readiness
  |     -> python -m pre_active ... daemon
  |
  +-- PreActive Watchdog
        -> every minute
        -> restarts either supervisor task when it is not Running
```

The long-running tasks use `SYSTEM`, highest run level, no execution-time limit, battery-safe settings, `IgnoreNew` duplicate suppression, and Task Scheduler restart settings. The watchdog is a second recovery layer because Task Scheduler restart-on-failure behavior alone was not sufficient in fault injection.

## GPU coexistence

The qualified host has a 4 GiB RTX 3050 Laptop GPU. The Qwen supervisor must not assume exclusive GPU ownership.

`start-qwen.ps1` checks free VRAM before model load. The initial Lappy binding uses a threshold of 3400 MiB. When another legitimate model-training/evaluation process owns the GPU, the Qwen supervisor remains alive and logs `WAIT_GPU` instead of repeatedly loading and crashing.

This was exercised while a separate H07/V2 trained-adapter evaluation occupied the GPU.

## Qualification evidence

The following behaviors were observed on the bound host:

1. The cached Qwen base model loaded locally under CUDA and returned the exact probe text `PRE_ACTIVE_QWEN_READY`.
2. The loopback HTTP shim exposed `GET /v1/models` and `POST /v1/chat/completions`.
3. A direct durable Pre-Active run through that HTTP path completed with `PRE_ACTIVE_HTTP_OK`.
4. A persistent installed runtime under `C:\ProgramData\PreActive` completed `INSTALLED_PRE_ACTIVE_OK`.
5. The registered Task Scheduler path completed `STARTUP_TASK_RUNTIME_OK`.
6. Killing the Qwen supervisor task and invoking the watchdog restarted the supervisor.
7. While the GPU was intentionally occupied by another model evaluation, a submitted Pre-Active run remained durable through repeated connection failures.
8. That same run was not resubmitted. After GPU release, the Qwen supervisor observed sufficient VRAM, started the endpoint, and the existing Pre-Active run completed automatically with `GPU_CONTENTION_RECOVERED`.
9. The recovered event completed on its tenth attempt, demonstrating durable retry across a prolonged model-backend outage.
10. Lappy subsequently rebooted at `2026-09-25T17:53:06.5-04:00`; both boot-triggered supervisors returned under `SYSTEM`, the local model endpoint recovered, and the installed source read back at current `main@2ed253c1b78af1cc9bd163e346d20f2fd86348f5`.
11. A fresh task submitted only after that reboot completed as `POST_REBOOT_RUNTIME_OK` (`ccf0013c-10e9-41e6-a15a-64a5edfd93ae`) with status `COMPLETED`, step 1, and `last_error = NULL`.
12. The host persists a machine-readable qualification record at `C:\\ProgramData\\PreActive\\runtime\\QUALIFICATION.json`; the record written after the post-reboot probe had SHA-256 `4A710EBF6AF3814C3EB11DD324BB86D219287E5E38EA62E588CBD1471C0F5F7F`.
13. The native Qwen tool-call bridge returned a structured OpenAI-compatible `tool_calls` response for `math.double({"value":6})` on the exact payload that previously returned fenced pseudo-JSON as ordinary text.
14. A full Pre-Active engine run (`aeee4782-cb3a-4adf-9ad5-53eced739942`) executed the non-mutating `math.double` tool, durably recorded `{"result": 12}`, advanced to step 2, and completed with exact final text `TOOL_LOOP_OK:12` and `last_error = NULL`.

## Failures found during qualification

Qualification found and corrected several host/runtime assumptions:

- A temporary qualification venv was not acceptable as a persistent dependency; Python and inference packages were pinned beneath the Pre-Active root.
- PowerShell `$ErrorActionPreference = "Stop"` can turn harmless native stderr warnings into `NativeCommandError`; child processes are therefore launched with `Start-Process` and separate stdout/stderr files.
- Default scheduled-task configuration would have stopped long-running tasks after 72 hours and had no useful self-heal path; those settings were hardened.
- Task Scheduler restart-on-failure did not reliably recover the long-running wrapper in fault injection, so an explicit recurring watchdog was added.
- GPU contention with legitimate parallel model work can kill or prevent model startup on a 4 GiB device; the Qwen supervisor now waits for sufficient free VRAM.
- A completed run after backend recovery retained the previous transient provider error in `last_error`. That is a state-quality defect and should be fixed separately in the Pre-Active kernel.
- Qwen can emit a valid native `<tool_call>...</tool_call>` envelope and then continue with role scaffolding or premature final text. The bridge commits to the first complete tool call, discards only non-tool trailing chatter, and still fails closed if the suffix contains any additional tool-protocol markup.

## Current capability ceiling

The current Qwen HTTP shim qualifies text completion **and one structured model-originated tool call per model turn** through Pre-Active's existing OpenAI-compatible adapter. The parser is fail-closed for unknown tools, malformed envelopes, duplicate/unknown parameters, invalid typed values, parallel calls, and additional tool markup after the committed call.

A full end-to-end non-mutating tool loop has been observed. Mutation-tool execution remains governed by Pre-Active's durable effect/idempotency machinery, but a local-Qwen mutation tool has not yet been qualified end-to-end on this host. That is the next effect-boundary qualification frontier.

The host is bound to the base Qwen revision listed above. A separately trained Vera/H07 adapter must not be substituted merely because its files exist; it requires its own completed behavioral qualification and an explicit runtime binding.

## Operational files

`ops/windows/` contains:

- `qwen_http.py` — loopback-only OpenAI-compatible text/tool-call shim;
- `tool_protocol.py` — fail-closed native Qwen tool-envelope parser and OpenAI tool-call renderer;
- `start-qwen.ps1` — GPU-aware model supervisor;
- `start-pre-active.ps1` — Pre-Active daemon supervisor;
- `watchdog.ps1` — supervisor task watchdog;
- `register-tasks.ps1` — scheduled-task registration.

The host also requires `runtime\model-path.txt`, containing the exact local model snapshot path. Keeping the model location as host configuration avoids silently baking a machine-specific cache path into source.

## Rename qualification boundary

This source rename does not migrate the Lappy installation, Windows scheduled tasks, environment, state database, or model process. Any claim that the host is currently running Pre-Active requires a separate installation/cutover action and fresh runtime qualification against the renamed paths and task identities.
