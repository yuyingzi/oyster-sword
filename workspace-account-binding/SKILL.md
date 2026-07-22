---
name: workspace-account-binding
description: Bind CLI accounts to workspaces so agents always operate on the right tenant. Use when one machine has a single-config CLI (lark-cli, gh, vault-style tools) authed against multiple accounts/orgs, and you want each project directory to determine which account agents use — with a verified switch command instead of env hacks or config-file swapping. Triggers - "switch account per project", "agent used the wrong tenant", "multi-account CLI".
---

# Workspace-bound account switching

One CLI, multiple accounts (work / personal, different orgs), agents running in many projects. Without a mechanism, whichever account was last active silently receives every operation — including writes to the wrong tenant.

The pattern: **the workspace declares the account; agents verify-then-switch before every operation; switching is one audited command that only touches the CLI's own config structure.**

## The four parts

### 1. Single config, all accounts registered

Register every account inside the CLI's own config file (many CLIs already store an array and use the first entry — e.g. lark-cli reads `apps[0]`). Secrets and tokens stay wherever the CLI keeps them (macOS Keychain); the config file holds only metadata. No parallel config files, no backups that go stale.

### 2. A `<cli>-switch` script

A tiny script on PATH (`~/.claude/bin/`) with exactly three verbs:

```
lark-switch status      # print active account + registered spares
lark-switch work        # atomic reorder → verify → echo identity
lark-switch personal
```

Rules the script enforces:
- **Atomic write** (tmp file + rename) of the reordered config.
- **Post-switch verification**: re-run the CLI's own `config show` and confirm the expected app id — never trust the write blindly.
- **Never call the CLI's lifecycle commands** (`config init` / `config remove`). See pitfalls.

Reference implementation: [`lark-switch.example`](lark-switch.example) — copy to `~/.claude/bin/<cli>-switch`, fill in your own `ACCOUNTS` map (app ids are client-id-like identifiers, safe to hardcode; **secrets never appear in the script or config — they live in the OS keychain**).

### 3. Workspace declaration

Agent instruction files declare the binding where agents already look:

- Global rule (e.g. `~/dev/CLAUDE.md`): *"Before any Feishu operation run `lark-switch status`; if it mismatches the workspace declaration, switch. Undeclared workspaces default to `work`."*
- Per-project CLAUDE.md / AGENTS.md / project memory: one line — *"This workspace uses the `personal` account."*

A default-plus-override scheme means company repos need zero configuration; only personal projects carry a declaration.

### 4. Agent contract

- `status` check before the first operation of a task; switch on mismatch.
- A chat instruction ("save this to my personal KB") overrides the workspace default for that operation.
- Wrong-channel operations fail loudly (permission / keychain errors), so a missed switch surfaces immediately instead of writing to the wrong tenant — resource-level permissions are the backstop, not the mechanism.

## Pitfalls (paid for)

- **`HOME`-override fake homes**: tempting (per-invocation, stateless) but rejected — `HOME` affects everything the process resolves from the home directory, and it produced unexplainable failures. Don't build switching on an env var the tool never promised to honor.
- **`cp` config-file swapping**: rejected — stale backup copies, races between sessions, and "which file is live?" ambiguity. Reordering *inside* the single live file keeps one source of truth.
- **Lifecycle commands wipe credentials**: `lark-cli config init` deletes keychain entries of apps it replaces. An agent innocently re-initing cost a work-account secret. Hence the hard ban in the switch script and in agent instructions.
- **Secret re-entry is human-only**: if a secret is lost, the *human* retrieves it from the developer console and runs `config init --app-id <id> --app-secret-stdin` **in their own terminal** (stdin → no shell history, no process list, never through the agent). Before running, temporarily shrink the config to only that app so init cannot touch the other account's keychain entries; re-add the other app afterwards.
- **Global active state**: last switch wins. Serialize sessions that need different accounts; don't run cross-account Feishu operations in parallel.

## Limits

Per-invocation account selection is impossible without native profile support (no `--profile`, no env var — verified by reading the binary). This pattern is the smallest reliable substitute; if the CLI ever ships profiles, migrate to them and delete the script.
