# Repo Sweep

Find bugs across your repositories and see what’s worth fixing in one evidence-backed report.

![A cheerful desktop computer sends beams of light to a server, browser and book.](assets/banner.jpg)

## Install

On macOS 13 or later, install with Homebrew or [download the signed and notarized DMG](https://github.com/nanomader/repo-sweep/releases/latest).

```sh
brew install --cask nanomader/tap/repo-sweep
```

## Run a sweep

Point a signed-in Claude Code (`cc`) or Codex CLI at a repository or a folder of repositories. Either agent can run either schedule.

```sh
repo-sweep cc ~/code daily
repo-sweep codex ~/code weekly
```

- **Daily** looks for regressions in committed changes since the last completed daily sweep. It exits early when nothing changed. [Daily prompt](daily.md)
- **Weekly** follows workflows across repositories, including unchanged code. It can trace a failure from a mobile client to the API it calls. [Weekly prompt](weekly.md)

Open the local HTML report at the path printed by the agent. Skim findings by severity, see the evidence behind each confidence rating, and copy a brief for one, selected, or all findings.

## Model options

| Agent | Default (lower cost) | With `--premium-model` |
| --- | --- | --- |
| Claude Code | Opus 5.5 | Fable 5.2 |
| Codex | GPT-6.1 Sol | GPT-6 Astra |

All models use `xhigh` reasoning. To use a premium model, append the flag:

```sh
repo-sweep codex ~/code weekly --premium-model
```

## Access and privacy

The agent runs with permission prompts disabled. Hosted agents send the source they read to their provider.

Each sweep reads relevant review guidance from pinned GitHub revisions. If a source is unavailable, the report identifies the coverage gap.

[License](LICENSE) · [Source notices](THIRD_PARTY.md)
