# ChatGPT plugin

Status: private control-surface candidate.

The repository contains an installable ChatGPT plugin package under
`plugin/pre-active/`. The plugin is intentionally a control and diagnostic
surface over the real host-side continuous runtime; it is not a second execution engine.

The control surface preserves Pre-Active's defining autonomy semantics: a user prompt is only one possible cause of a model turn, promptless turns may be granted by authorized observation/time/open-loop/endogenous sources, and autonomous cognition never manufactures effect authority.

## Transport

When Workbridge Commander is installed and reachable, the skill uses that
workstation transport to establish live state and operate the runtime. The
plugin package does not embed Workbridge credentials and does not claim that
loading the plugin creates a workstation connection.

## Current host observation

A read-only check on 2026-10-01 found that the documented current
`C:\ProgramData\PreActive` binding and `PreActive*` scheduled tasks were
absent on Lappy while the configured local model endpoint on port 18081 was
alive under a predecessor installation.

That observation is intentionally not promoted into source truth. The plugin
must rediscover runtime state on each materially relevant operation.

## Authority boundary

Read-only diagnosis may proceed directly when requested. Submitting or
scheduling durable work authorizes only that exact durable state mutation.
Machine installation, runtime migration, scheduled-task registration,
process/service replacement, credential changes, or model cutover remain
separate host effects and require explicit authorization.

## Package contents

```text
plugin/pre-active/
  plugin.json
  skills/pre-active/
    SKILL.md
    references/RUNTIME_CONTRACT.md
```

The private ChatGPT package is skills-first. It relies on an already available
workstation connector for local effects rather than embedding a second local
transport or copying workstation credentials into the package.
