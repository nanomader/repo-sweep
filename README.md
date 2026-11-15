# Repo Sweep

![A cheerful desktop computer sends beams of light to a server, a browser and an open book.](assets/banner.jpg)

Review the Git checkouts under one folder. The local HTML report lists findings by severity and lets you copy a fix brief for one, selected or all findings.

## Run a daily review

From the Repo Sweep checkout, pass one Git repository or the folder containing your repositories. You need macOS or Linux, Python 3.9+, Git and a current [Codex CLI](https://github.com/openai/codex) or [Claude Code](https://code.claude.com/docs/en/cli-reference).

```sh
./sweep.py /path/to/repositories daily --list  # Preview the checks without calling an AI model
./sweep.py /path/to/repositories daily         # Run them now
```

For weekly, use `./sweep.py /path/to/repositories weekly`. Either command accepts `--list`. Open the report path printed on the last line. Exit code 2 means coverage was incomplete.

## Which prompts run?

| Schedule | What the checks review |
| --- | --- |
| [Daily prompts](prompts/daily.md) | Committed changes since the last complete daily review on each branch. The first run covers the last 24 hours. A failed check keeps the same starting commit for the retry. |
| [Weekly prompts](prompts/weekly.md) | The current commit in every checkout, including unchanged code. |

Both ship with 11 checks covering correctness, reliability, deployment, dependencies, test health, performance, frontend user journeys, security, interfaces, incidents and variants of known bugs. **Edit the linked list under Checks in either schedule to change what that command runs.**

Each check runs once across the folder with the shared policy, up to three at a time. Each saves a Markdown note. One final prompt verifies and merges their findings. The catalog combines our instructions with reviewed excerpts from Cloudflare, Sentry, OpenAI, Anthropic, Trail of Bits, GitHub Next and Supabase. [Shared prompts and pinned sources](prompts.md)

## Choose an agent

Sign in to Codex (the default) or set `SWEEP_AGENT=claude` for Claude Code. Add `SWEEP_MERGE_AGENT=codex` to consolidate Claude's notes with Codex. Hosted agents send reviewed source to their provider and use your plan or API billing. For local inference, start Ollama with a downloaded tool-capable model and set `SWEEP_AGENT=ollama SWEEP_MODEL=gpt-oss:20b`.

Reviews cover committed UTF-8 text files up to 2 MiB each. Optional redacted logs or advisories go in `.repo-sweep-evidence/` under the supplied folder. Reports and notes live under `${XDG_STATE_HOME:-~/.local/state}/repo-sweep/`. [Code license](LICENSE) · [Prompt licenses](THIRD_PARTY.md)
