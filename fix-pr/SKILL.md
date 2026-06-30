---
name: fix-pr
description: Start a session-scoped heartbeat automation that polls a GitHub PR on a fixed interval and keeps it merge-ready — resolving conflicts (merge over rebase), addressing review comments, triggering reviews, and fixing failing workflows, then self-closes when no work remains. Use when the user wants to fix/babysit/watch a PR, set up a PR heartbeat, keep a PR green/merge-ready, or says "fix-pr" / "heartbeat" / "babysit PR #N" / "watch this PR".
---

# PR Heartbeat

Spins up a **session-scoped** heartbeat (`codex_app.automation_update`, `kind=heartbeat`) that polls one PR on an interval and drives it toward merge-ready, then shuts itself off.

## Quick start

Required: the PR (number or URL). Everything else defaults.

Create the heartbeat with:
- **model**: GPT-5.5 （if you are claude, use Opus-4.7)
- **reasoning**: Medium
- **interval**: every 10 minutes
- **target**: the PR the user named

Defaults are overridable — if the user gives a model/reasoning/interval, use theirs.

## What each tick does

Run these checks in order on the PR:

1. **Conflicts first.** If the PR conflicts with base, **merge the base/remote changes in — do NOT rebase**. Prefer pulling remote changes over rewriting history.
2. **Deploy logs (if relevant).** If the PR involves deployment, read the matching logs from SLS via `aliyun-cli` to diagnose.
3. **Review comments.**
   - Read the PR description's emoji reactions: an **eyes 👀** reaction means codex/cursor is *currently* reviewing — let it finish.
   - If there are new review comments, **address them**; on each resolved thread, **reply and mark it resolved**.
   - If at check time there is **no 👀 and no 👍 (+1)**, trigger a review by commenting **`@codex review`**.
4. **Workflows.** If any workflow run failed, **fix it**.

## Self-close condition

When a tick finds **no remaining work** AND **all reviews are done with no outstanding review feedback**, close this heartbeat automation.

A PR status of **"Review Required"** means it's waiting on a human — that is a safe point to close the heartbeat.

## Notes

- Session-scoped: the automation lives with the current session, not a global cron.
- One heartbeat per PR. If asked to watch another PR, start a separate heartbeat.
