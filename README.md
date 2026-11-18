# Oyster sword

My personal collection of the most useful AI coding skills and workflows.

## What's here

### `ponytail/` (git submodule)
The lazy senior dev plugin. Enforces YAGNI, stops over-engineering, ships the shortest working diff.
Source: [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)

### `engineering/`
Matt Pocock's engineering skill set for AI agents — TDD, architecture design, bug diagnosis, prototyping, domain modeling, and more.
Source: [mattpocock/skills](https://github.com/mattpocock/skills)

### `fix-pr/`
PR heartbeat skill — polls a GitHub PR on an interval, resolves conflicts, addresses review comments, fixes failing CI, and self-closes when no work remains.

### `workspace-account-binding/`
Multi-account CLI switching for agents — each workspace declares which account (work/personal) it uses, agents verify-then-switch via one audited command. No env hacks, no config-file swapping; includes the hard-won pitfalls (lifecycle commands wiping keychain secrets, human-only secret re-entry).

### `codex-deepseek-profile/`
Run Codex with `deepseek-v4-flash` through an isolated profile while keeping the default OpenAI configuration unchanged. Includes a safe installer, status/uninstall commands, and an isolated install test.

### `llm-batch-template/`
Reusable Python template for concurrent LLM batch jobs with rate limits, retries, SQLite checkpoints, and progress monitoring. See the [template README](llm-batch-template/README.md).

## Setup

```bash
# Clone with submodules
git clone --recurse-submodules https://github.com/yuyingzi/oyster-sword.git

# Or if already cloned
git submodule update --init
```
