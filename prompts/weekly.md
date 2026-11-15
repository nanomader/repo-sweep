# Weekly review

Each check below runs once across the supplied repository folder, in parallel when available. Each receives the shared policy. [Policy — evidence and reporting](../prompts.md#policy--evidence-and-reporting)

## Scope

Review the current commit of every checkout, including unchanged code. Inspect every repository where the assigned check applies. Trace callers, contracts and deployment settings across repositories when they establish a finding.

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
