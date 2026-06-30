# Oyster knife

A personal collection of the most useful AI coding skills and workflows.

## What's here

### `ponytail/` (git submodule)
The lazy senior dev plugin. Enforces YAGNI, stops over-engineering, ships the shortest working diff.
Source: [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)

### `engineering/`
Matt Pocock's engineering skill set for AI agents — TDD, architecture design, bug diagnosis, prototyping, domain modeling, and more.
Source: [mattpocock/skills](https://github.com/mattpocock/skills)

### `fix-pr/`
PR heartbeat skill — polls a GitHub PR on an interval, resolves conflicts, addresses review comments, fixes failing CI, and self-closes when no work remains.

## Setup

```bash
# Clone with submodules
git clone --recurse-submodules https://github.com/yuyingzi/oyster-knife.git

# Or if already cloned
git submodule update --init
```
