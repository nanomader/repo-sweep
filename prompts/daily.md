# Daily review

Each check below runs once across the supplied repository folder, with up to three checks running at a time. Each receives the shared policy. [Policy — evidence and reporting](../prompts.md#policy--evidence-and-reporting)

On a day with no committed changes in scope, the runner still launches the listed checks and asks each to record a skip.

## Scope

Review committed changes since the last completed daily run on each branch. On the first run, use the last 24 hours. If a check fails, retry from the same starting commit. If history was rewritten and the saved commit is no longer an ancestor, review the current tree. Read the diffs in `changes/*.patch` and follow callers across repositories. Report regressions introduced or exposed by those changes and still present at the reviewed commit. Unchanged repositories can supply context.

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

Once all Markdown notes are available, run the final prompt once to verify, merge and report findings. [Consolidate — verify findings and write the report](../prompts.md#consolidate--verify-findings-and-write-the-report)
