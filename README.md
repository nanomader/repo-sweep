# Repo Sweep

![A cheerful desktop computer sends beams of light to a server, a browser and an open book.](assets/banner.jpg)

Review every Git checkout under one folder. Open the local HTML report, skim findings by severity and copy a fix brief for one, selected or all findings. Each brief includes the evidence, exact revisions and lines, a repair direction and a regression check.

## Run

On macOS or Linux, use Python 3.9+, Git and a current [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://code.claude.com/docs/en/cli-reference). Sign in to use a hosted model.

```sh
./sweep.py /path/to/repositories daily
./sweep.py /path/to/repositories weekly
```

Daily reviews committed changes since the last complete daily run on each branch (24 hours on the first run). Weekly reviews the current commits, including unchanged repositories. Both run the same checks concurrently, save one Markdown note per check, then run one consolidation prompt. The last output line is the report path. Exit code 2 means coverage was incomplete.

Codex is the default. Set `SWEEP_AGENT=claude` for Claude Code. Add `SWEEP_MERGE_AGENT=codex` to have Codex verify and merge Claude's findings. Hosted models receive the source they read and use your plan or API billing. For local inference, start Ollama with a downloaded, tool-capable model and set `SWEEP_AGENT=ollama SWEEP_MODEL=gpt-oss:20b` for both stages.

[Browse the prompts and pinned sources](prompts.md). The checks use selected review instructions from Cloudflare, Sentry, OpenAI, Anthropic, Trail of Bits, GitHub Next and Supabase, plus our shared evidence policy and a few original prompts. They ship as text in this repository. Runs do not install upstream skills.

Reviews cover committed UTF-8 text files up to 2 MiB each. Optional redacted CI logs, advisories or incident notes go in `.repo-sweep-evidence/` under the supplied folder. Reports and Markdown notes are private local files under `${XDG_STATE_HOME:-~/.local/state}/repo-sweep/`. [Code license](LICENSE) · [Prompt licenses](licenses/README.md)
