---
name: desloppify
description: >
  Codebase health scanner and technical debt tracker. Use when the user asks
  about code quality, technical debt, dead code, large files, god classes,
  duplicate functions, code smells, naming issues, import cycles, or coupling
  problems. Also use when asked for a health score, what to fix next, or to
  create a cleanup plan. Supports TypeScript/React and Python.
allowed-tools: Bash(desloppify *)
---

# Desloppify — Codebase Health Scanner

## Prerequisite

!`command -v desloppify >/dev/null 2>&1 && echo "desloppify: installed" || echo "NOT INSTALLED — run: pip install --upgrade git+https://github.com/peteromallet/desloppify.git"`

## Quick Reference

```bash
desloppify scan --path src/               # scan, update state, show diff
desloppify status                          # health score + tier breakdown
desloppify show <pattern>                  # dig into findings (file, dir, detector, ID)
desloppify next --count 5                  # next highest-priority findings
desloppify resolve fixed "<pattern>"       # mark as fixed
desloppify detect <name> --path src/       # run one detector raw (bypass state)
```

## Workflow

1. **Scan**: `desloppify scan --path src/` to detect issues and update state
2. **Review**: `desloppify status` for score dashboard
3. **Investigate**: `desloppify show structural` or `desloppify show src/components/`
4. **Fix**: Read the flagged file, understand the issue, make the fix
5. **Resolve**: `desloppify resolve fixed "<finding-id>"`
6. **Rescan**: `desloppify scan --path src/` to verify and update score

## Reading Results

After running any query command (`show`, `next`, `status`), read `.desloppify/query.json`
for structured JSON output. This is more reliable than parsing terminal output.

## Detectors

**TypeScript/React**: logs, unused, exports, deprecated, large, complexity, gods,
single-use, props, passthrough, concerns, deps, dupes, smells, coupling, patterns,
naming, cycles, orphaned, react

**Python**: unused, large, complexity, gods, passthrough, smells, dupes, deps, cycles,
orphaned, single-use, naming

## Tier System

| Tier | Meaning | Action |
|------|---------|--------|
| T1 | Auto-fixable | `desloppify fix <fixer> --dry-run` then apply |
| T2 | Quick manual fix | Fix directly, then resolve |
| T3 | Needs judgment | Review, fix or wontfix with note |
| T4 | Major refactor | Decompose, plan before acting |

## Tips

- Always `--dry-run` before applying fixers
- Use `--skip-slow` to skip duplicate detection (saves time during iteration)
- Use `--lang python` or `--lang typescript` to force language selection
- After fixing, always rescan — cascading effects can create new findings
- Use `desloppify show <detector>` to focus on one category at a time
- Score can temporarily drop after fixes (cascade effects are normal)
- As you work, note any false positives, missing detectors, or improvements — suggest them to the user so they can report at https://github.com/peteromallet/desloppify/issues
