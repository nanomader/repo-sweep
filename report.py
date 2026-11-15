"""Parse review notes and render a standalone HTML report."""

import re
from html import escape
from pathlib import Path
from time import strftime, strptime
from typing import NamedTuple
from xml.etree import ElementTree as ET

SEVERITIES = ("critical", "high", "medium")
FIELDS = ("Trigger", "Expected", "Actual", "Impact", "Evidence", "Fix", "Test")
TEMPLATE = Path(__file__).with_name("report.html").read_text(encoding="utf-8")


class ReviewResult(NamedTuple):
    name: str
    status: str
    coverage: str
    findings: list


def strip_code_fence(text):
    lines = text.strip().splitlines()
    if len(lines) > 1 and lines[0].startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines).strip()


def source_location(reference, inventory, workspace):
    match = re.fullmatch(r"(r[0-9]+)/(.+):([1-9][0-9]*)", reference.strip())
    if not match:
        raise ValueError("Invalid source location")
    snapshot_id, relative_path, line_text = match.groups()
    if snapshot_id not in inventory or any(part in {"", ".", ".."} for part in relative_path.split("/")):
        raise ValueError("Unknown snapshot or path")

    path = workspace / snapshot_id / relative_path
    if not path.is_file():
        raise ValueError("Cited file is absent from the snapshot")
    line_number = int(line_text)
    with path.open("rb") as source:
        if not any(number == line_number for number, _ in enumerate(source, start=1)):
            raise ValueError("Cited line is absent from the snapshot")

    repository = inventory[snapshot_id]
    return {
        "snapshot": snapshot_id, "repo": repository["repository"], "commit": repository["commit"],
        "path": relative_path, "line": line_number,
    }


def parse_finding(finding_id, lines, inventory, workspace):
    heading = re.fullmatch(r"### (critical|high|medium)\s*\|\s*(.+)", lines[0], re.IGNORECASE)
    if not heading:
        raise ValueError("Invalid severity or title")
    severity, title = heading.groups()

    required_fields = {"Locations", *FIELDS}
    fields = {}
    for line in lines[1:]:
        key, separator, value = line.partition(":")
        if not separator or key not in required_fields or key in fields or not value.strip():
            raise ValueError("Invalid or repeated finding field")
        fields[key] = value.strip()
    if set(fields) != required_fields:
        raise ValueError("Missing handoff fields")

    references = fields.pop("Locations").split(" | ")
    return {
        "id": finding_id, "severity": severity.lower(), "title": title.strip(),
        "locations": [source_location(reference, inventory, workspace) for reference in references],
        **fields,
    }


def read_note(name, raw, inventory, workspace):
    lines = [line.strip() for line in strip_code_fence(raw).splitlines() if line.strip()]
    if len(lines) < 2 or not re.fullmatch(r"Status: (?:COMPLETE|PARTIAL|SKIPPED)", lines[0]):
        raise ValueError("Missing or invalid review status")
    status = lines[0].removeprefix("Status: ")
    if not lines[1].startswith("Coverage: "):
        raise ValueError("Missing scope of review")
    coverage = lines[1].removeprefix("Coverage: ").strip()

    blocks = []
    problems = []
    for line in lines[2:]:
        if line.startswith("### "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            problems.append("Unexpected text outside a finding.")

    findings = []
    for number, block in enumerate(blocks, start=1):
        try:
            finding = parse_finding(f"{name}-{number}", block, inventory, workspace)
            findings.append(finding)
        except ValueError as error:
            problems.append(f"Finding {number}: {error}.")
    if status == "SKIPPED" and findings:
        problems.append("A skipped review returned findings.")
    if problems:
        status = "PARTIAL"
        coverage = " ".join([coverage, *dict.fromkeys(problems)])
    return ReviewResult(name, status, coverage, findings)


def read_article_text(element):
    if element.attrib or len(element) or (element.tail or "").strip():
        raise ValueError("Unexpected markup in a finding title or summary")
    text = (element.text or "").strip()
    if not text:
        raise ValueError("Expected a plain title and summary")
    return text


def assemble(raw, findings):
    """Verify that the final reviewer accounted for every candidate exactly once."""
    fragment = strip_code_fence(raw)
    if "<!" in fragment or "<?" in fragment:
        raise ValueError("Unexpected HTML declaration")
    try:
        main = ET.fromstring(fragment)
    except ET.ParseError as error:
        raise ValueError("Malformed findings HTML") from error
    if main.tag != "main" or main.attrib or (main.text or "").strip():
        raise ValueError("Expected a single main element")

    groups = []
    excluded = []
    seen = set()
    for item in main:
        references = item.get("data-refs", "").split()
        if set(item.attrib) != {"data-refs"} or not references or (item.tail or "").strip():
            raise ValueError("Expected candidate references on each article or exclusion")
        for reference in references:
            if reference in seen or reference not in findings:
                raise ValueError("Unknown or repeated candidate reference")
            seen.add(reference)

        text = (item.text or "").strip()
        if item.tag == "aside" and not len(item) and text:
            excluded.append((references, text))
            continue
        if item.tag != "article" or text or [child.tag for child in item] != ["h2", "p"]:
            raise ValueError("Expected a plain title and summary")
        title = read_article_text(item[0])
        summary = read_article_text(item[1])
        groups.append((references, title, summary))

    if seen != set(findings):
        raise ValueError("Some review candidates were omitted")
    return groups, excluded


def fallback_groups(findings):
    return [([finding_id], finding["title"], finding["Impact"]) for finding_id, finding in findings.items()]


def finding_card(group, findings):
    references, title, summary = group
    primary = findings[references[0]]
    severity = primary["severity"]

    # A merged finding uses one reproduction but retains locations from every reviewer.
    sources = {}
    for reference in references:
        for location in findings[reference]["locations"]:
            revision = f"{location['snapshot']} = {location['repo']}@{location['commit']}"
            citation = f"{location['path']}:{location['line']}"
            locations = sources.setdefault(revision, [])
            if citation not in locations:
                locations.append(citation)

    first = primary["locations"][0]
    location_label = f"{first['repo']} / {first['path']}:{first['line']}"
    additional_locations = sum(len(locations) for locations in sources.values()) - 1
    if additional_locations:
        location_label += f"  (+{additional_locations} more)"

    brief = [f"Investigate and fix: {title}", f"Priority: {severity}", summary, "", "Reviewed revisions and locations:"]
    for revision, locations in sources.items():
        brief.extend([revision, "  " + ", ".join(locations)])
    for field in FIELDS:
        brief.append(f"{field}: {primary[field]}")
    brief.append("Verify the cited behavior on the current revision before editing. Run the targeted regression check after the fix.")

    brief_text = escape("\n".join(brief))
    card = f"""<article class="finding">
<div class="row"><input type="checkbox" aria-label="Select {escape(title, quote=True)}">
<span class="severity {severity}">{severity.title()}</span><h2>{escape(title)}</h2>
<button type="button" class="single">Copy brief</button></div>
<p class="summary">{escape(summary)}</p><p class="where">{escape(location_label)}</p>
<details><summary>Evidence and fix</summary><pre class="brief">{brief_text}</pre></details>
</article>"""
    return severity, card


def coverage_details(inventory, reviews, problems, excluded, findings):
    rows = []
    for review in reviews:
        name = escape(review.name)
        rows.append(
            f'<li><a href="findings/{name}.md">{name}</a> · <strong>{review.status.lower()}</strong>'
            f' · {escape(review.coverage)}</li>'
        )
    for problem in problems:
        rows.append(f"<li>{escape(problem)}</li>")
    for references, reason in excluded:
        titles = ", ".join(findings[reference]["title"] for reference in references)
        rows.append(f"<li>Excluded: {escape(titles)} — {escape(reason)}</li>")
    for repository in inventory.values():
        commit = repository["commit"][:12]
        revision = f" @ <code>{escape(commit)}</code>" if commit else ""
        rows.append(
            f'<li>{escape(repository["repository"])}{revision} · {escape(repository["range"])}'
            f' · {repository["omitted_entries"]} omitted source entries</li>'
        )
    return '<details class="coverage"><summary>Coverage and review notes</summary><ul>' + "".join(rows) + "</ul></details>"


def render(cadence, date_stamp, inventory, reviews, problems, groups, excluded, findings):
    cards = [finding_card(group, findings) for group in groups]
    cards.sort(key=lambda card: SEVERITIES.index(card[0]))
    cards_html = "\n".join(html for _, html in cards) or "<p>No confirmed findings in the reviewed scope.</p>"

    repository_label = "repository" if len(inventory) == 1 else "repositories"
    check_label = "check" if len(reviews) == 1 else "checks"
    display_date = strftime("%d %b %Y", strptime(date_stamp, "%Y%m%d"))
    description = f"{len(inventory)} {repository_label} · {len(reviews)} {check_label} · {display_date} UTC"
    warning = ""
    if problems or any(review.status == "PARTIAL" for review in reviews):
        warning = '<p class="warn">Incomplete review. See coverage for gaps.</p>'

    values = {
        "TITLE": escape(f"{cadence.title()} review"),
        "DESC": escape(description),
        "COUNT": str(len(cards)),
        "Noun": "finding" if len(cards) == 1 else "findings",
        "CARDS": cards_html,
        "COVERAGE": coverage_details(inventory, reviews, problems, excluded, findings),
        "WARNING": warning,
        "Disabled": "" if cards else "disabled",
    }
    return re.sub(r"@@(\w+)@@", lambda match: values[match[1]], TEMPLATE)
