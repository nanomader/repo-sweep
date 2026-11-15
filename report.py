"""Parse review notes and render a standalone HTML report."""
from datetime import datetime
from html import escape
from pathlib import Path
import re
from xml.etree import ElementTree as ET

SEVERITIES = ("critical", "high", "medium")
FIELDS = ("Trigger", "Expected", "Actual", "Impact", "Evidence", "Fix", "Test")
TEMPLATE = Path(__file__).with_name("report.html").read_text(encoding="utf-8")


def unfence(text):
    lines = text.strip().splitlines()
    if len(lines) > 1 and lines[0].startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines).strip()


def location(value, index, work):
    parts = re.fullmatch(r"(r[0-9]+)/(.+):([1-9][0-9]*)", value.strip())
    if not parts:
        raise ValueError("Invalid source location")
    key, name, number = parts.groups()
    if key not in index or any(p in {"", ".", ".."} for p in name.split("/")):
        raise ValueError("Unknown snapshot or path")
    file = work / key / name
    if not file.is_file():
        raise ValueError("Cited file is absent from the snapshot")
    with file.open("rb") as source:
        if not any(i == int(number) for i, _ in enumerate(source, 1)):
            raise ValueError("Cited line is absent from the snapshot")
    return {"snapshot": key, "repo": index[key]["repository"], "commit": index[key]["commit"], "path": name, "line": int(number)}


def read_note(name, raw, index, work):
    lines = [s.strip() for s in unfence(raw).splitlines() if s.strip()]
    if len(lines) < 2 or not re.fullmatch(r"Status: (?:COMPLETE|PARTIAL|SKIPPED)", lines[0]):
        raise ValueError("Missing or invalid review status")
    if not lines[1].startswith("Coverage: ") or not lines[1][10:].strip():
        raise ValueError("Missing scope of review")
    status, coverage = lines[0][8:], lines[1][10:].strip()
    blocks, problems, items = [], [], []
    for line in lines[2:]:
        if line.startswith("### "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            problems.append("Unexpected text outside a finding.")
    for i, block in enumerate(blocks, 1):
        try:
            heading = re.fullmatch(r"### (critical|high|medium)\s*\|\s*(.+)", block[0], re.I)
            if not heading:
                raise ValueError("Invalid severity or title")
            fields = {}
            for text in block[1:]:
                key, sep, value = text.partition(":")
                if not sep or key not in ("Locations", *FIELDS) or key in fields or not value.strip():
                    raise ValueError("Invalid or repeated finding field")
                fields[key] = value.strip()
            if set(fields) != {"Locations", *FIELDS}:
                raise ValueError("Missing handoff fields")
            locations = [location(v, index, work) for v in fields.pop("Locations").split(" | ")]
            items.append(dict(id=f"{name}-{i}", severity=heading[1].lower(), title=heading[2].strip(),
                              locations=locations, **fields))
        except ValueError as exc:
            problems.append(f"Finding {i}: {exc}.")
    if status == "SKIPPED" and items:
        problems.append("A skipped review returned findings.")
    if problems:
        status, coverage = "PARTIAL", " ".join([coverage, *dict.fromkeys(problems)])
    return name, status, coverage, items


def assemble(raw, findings):
    """Accept only plain text and known IDs. Generated markup never enters the page."""
    raw = unfence(raw)
    if "<!" in raw or "<?" in raw:
        raise ValueError("Unexpected HTML declaration")
    try:
        main = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Malformed findings HTML") from exc
    if main.tag != "main" or main.attrib or (main.text or "").strip():
        raise ValueError("Expected a single main element")
    groups, excluded, used = [], [], set()
    for item in main:
        refs = item.get("data-refs", "").split()
        if (set(item.attrib) != {"data-refs"} or not refs or len(set(refs)) != len(refs)
                or any(ref not in findings or ref in used for ref in refs) or (item.tail or "").strip()):
            raise ValueError("Unknown, missing or repeated candidate reference")
        used.update(refs)
        if item.tag == "aside" and not len(item) and (item.text or "").strip():
            excluded.append((refs, item.text.strip()))
            continue
        if (item.tag != "article" or (item.text or "").strip() or [el.tag for el in item] != ["h2", "p"]
                or any(el.attrib or len(el) or not (el.text or "").strip() or (el.tail or "").strip() for el in item)):
            raise ValueError("Expected a plain title and summary")
        groups.append((refs, item[0].text.strip(), item[1].text.strip()))
    if used != set(findings):
        raise ValueError("Some review candidates were omitted")
    return groups, excluded


def fallback_groups(findings):
    return [([k], v["title"], v["Impact"]) for k, v in findings.items()]


def finding_card(group, findings):
    refs, title, summary = group
    members = [findings[k] for k in refs]
    primary = members[0]
    severity = primary["severity"]
    sources = {}
    for member in members:
        for loc in member["locations"]:
            revision = f"{loc['snapshot']} = {loc['repo']}@{loc['commit']}"
            sources.setdefault(revision, {})[f"{loc['path']}:{loc['line']}"] = None
    where = primary["locations"][0]
    anchor = f"{where['repo']} / {where['path']}:{where['line']}"
    extra = sum(len(paths) for paths in sources.values()) - 1
    if extra:
        anchor += f"  (+{extra} more)"
    brief = [f"Investigate and fix: {title}", f"Priority: {severity}", summary, "",
             "Reviewed revisions and locations:"]
    for revision, paths in sources.items():
        brief.extend([revision, "  " + ", ".join(paths)])
    for field in FIELDS:
        brief.append(f"{field}: {primary[field]}")
    brief.append("Verify the cited behavior on the current revision before editing. Run the targeted regression check after the fix.")
    return severity, f"""<article class="finding">
<div class="row"><input type="checkbox" aria-label="Select {escape(title, quote=True)}">
<span class="severity {severity}">{severity.title()}</span><h2>{escape(title)}</h2>
<button type="button" class="single">Copy brief</button></div>
<p class="summary">{escape(summary)}</p><p class="where">{escape(anchor)}</p>
<details><summary>Evidence and fix</summary><pre class="brief">{escape(chr(10).join(brief))}</pre></details>
</article>"""


def render(mode, stamp, index, results, problems, groups, excluded, findings):
    cards = [finding_card(g, findings) for g in groups]
    cards.sort(key=lambda card: SEVERITIES.index(card[0]))
    partial = bool(problems) or any(s == "PARTIAL" for _, s, _, _ in results)
    count = len(cards)
    title = f"{mode.title()} review"
    repos = "repository" if len(index) == 1 else "repositories"
    checks = "check" if len(results) == 1 else "checks"
    desc = f"{len(index)} {repos} · {len(results)} {checks} · {datetime.strptime(stamp, '%Y%m%d'):%d %b %Y} UTC"
    warning = '<p class="warn">Incomplete review. See coverage for gaps.</p>' if partial else ""
    rows = [f'<li><a href="findings/{escape(name)}.md">{escape(name)}</a> · <strong>{status.lower()}</strong>'
            f' · {escape(why)}</li>' for name, status, why, _ in results]
    details = [f"<li>{escape(p)}</li>" for p in problems]
    for refs, reason in excluded:
        titles = ", ".join(findings[k]["title"] for k in refs)
        details.append(f"<li>Excluded: {escape(titles)} — {escape(reason)}</li>")
    for repo in index.values():
        commit = f' @ <code>{escape(repo["commit"][:12])}</code>' if repo["commit"] else ""
        details.append(f'<li>{escape(repo["repository"])}{commit}'
                       f' · {escape(repo["range"])} · {repo["omitted_entries"]} omitted source entries</li>')
    coverage = f'<details class="coverage"><summary>Coverage and review notes</summary><ul>{"".join(rows+details)}</ul></details>'
    if not count:
        cards_text = "<p>No confirmed findings in the reviewed scope.</p>"
    else:
        cards_text = "\n".join(html for _, html in cards)
    values = dict(TITLE=escape(title), DESC=escape(desc), COUNT=str(count),
                  Noun="finding" if count == 1 else "findings", CARDS=cards_text, COVERAGE=coverage, WARNING=warning,
                  Disabled="disabled" if count == 0 else "")
    return re.sub(r"@@(\w+)@@", lambda m: values[m[1]], TEMPLATE)
