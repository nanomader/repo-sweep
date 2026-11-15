# Repo Sweep

![A cheerful desktop computer sends beams of light to a server, browser and book.](assets/banner.jpg)

Install on macOS 13 or later with Homebrew, or [download the signed and notarized DMG](https://github.com/nanomader/repo-sweep/releases/latest).

```sh
brew install --cask nanomader/tap/repo-sweep
```

Run either schedule with a signed-in Claude Code or Codex CLI:

```sh
repo-sweep cc ~/code daily
repo-sweep codex ~/code weekly
```

[Daily](daily.md) checks committed changes since the last completed daily sweep. It exits early when nothing changed. [Weekly](weekly.md) traces current workflows across repositories, including unchanged code. Each file contains its complete prompt and pinned sources.

The agent runs with permission prompts disabled, reviews committed code and prints the path to its local HTML report. Each finding includes severity, confidence with supporting evidence, and a copyable investigation brief. Hosted agents send the source they read to their provider. [License](LICENSE) · [Source notices](THIRD_PARTY.md)
