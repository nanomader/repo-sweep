# Repo Sweep

![A cheerful desktop computer sends beams of light to a server, browser and book.](assets/banner.jpg)

Run from this checkout with Bash, Git and a signed-in Claude Code (`cc`) or Codex CLI (`codex`).

```sh
./sweep.sh cc /path/to/repositories daily
./sweep.sh codex /path/to/repositories weekly
```

Pick either agent for either schedule. Supply one Git checkout or its parent folder.

| Schedule | Scope |
| --- | --- |
| [Daily prompts](prompts/daily.md) | Committed changes since the last completed shell sweep on each branch. The first run uses the last 24 hours and retains its starting point for retries. |
| [Weekly prompts](prompts/weekly.md) | Current committed source across the checkouts. |

Each listed check runs once and saves a Markdown note, with up to three running at a time. The same agent then verifies and merges the notes into a local HTML report with copy buttons for one, selected or all findings. Open the path printed at the end. Exit status 2 signals incomplete coverage or an incomplete report.

The [shared catalog](prompts.md) combines reviewed excerpts from seven publishers with our evidence policy and reporting instructions. Optional redacted logs and advisories go under `.repo-sweep-evidence/` in the supplied folder. Hosted agents send the source they read to their provider. [License](LICENSE) · [Source notices](THIRD_PARTY.md)
