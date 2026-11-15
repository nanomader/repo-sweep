# Daily review

Review the repositories in the current directory. Follow the [run instructions](../prompts.md#run-the-sweep) and use the scope and checklist below.

On a day with no committed changes in scope, record a skip for each listed check.

## Scope

Review committed changes since the last completed daily run on each branch. Persist the starting commit for each checkout and branch under `~/.local/state/repo-sweep/`. On the first run, use the commit before the last 24 hours, or the empty tree for a newer repository. Advance the saved commits only after a complete report with no partial coverage. If a check fails, retry from the same starting commit. If history was rewritten and the saved commit is no longer an ancestor, review the current tree. Use Git to compare the starting commit with the recorded HEAD and follow callers across repositories. Report regressions introduced or exposed by those changes and still present at the reviewed commit. Unchanged repositories can supply context. If every patch is empty, return SKIPPED.

## Checks

- [Correctness — wrong results for valid inputs](../prompts.md#correctness--wrong-results-for-valid-inputs)
- [Reliability — lost or repeated operations](../prompts.md#reliability--lost-or-repeated-operations)
- [Deployment — migrations and mixed versions](../prompts.md#deployment--migrations-and-mixed-versions)
- [Dependencies — affected builds and releases](../prompts.md#dependencies--affected-builds-and-releases)
- [Test health — gates that pass broken behavior](../prompts.md#test-health--gates-that-pass-broken-behavior)
- [Performance — costly requests and jobs](../prompts.md#performance--costly-requests-and-jobs)
- [User journeys — broken frontend workflows](../prompts.md#user-journeys--broken-frontend-workflows)
- [Security — reachable trust-boundary violations](../prompts.md#security--reachable-trust-boundary-violations)
- [Interfaces — contracts between systems](../prompts.md#interfaces--contracts-between-systems)
- [Incidents — a failure documented in local evidence](../prompts.md#incidents--a-failure-documented-in-local-evidence)
- [Variants — other instances of a known bug](../prompts.md#variants--other-instances-of-a-known-bug)

## After the checks

After every check has finished or been recorded as partial, [generate the local HTML report](../prompts.md#consolidate--verify-findings-and-write-the-report) once and print its absolute path.
