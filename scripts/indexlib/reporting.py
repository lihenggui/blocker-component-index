"""Rule-compatible substring coverage, grouped only for presentation."""

from collections import Counter
from .storage import checkpointed, read_json


def group_label(group_id, overrides):
    matches = [(len(prefix), label) for label, prefixes in overrides.items() for prefix in prefixes if group_id.startswith(prefix)]
    return sorted(matches, key=lambda item: (-item[0], item[1]))[0][1] if matches else group_id


@checkpointed
def group(store, client, **_):
    client.check()
    # Grouping is computed by the report; it adds nothing to the evidence schema.
    store.config("sdk-overrides.yaml")
    store.save()
    return True


def matching_rules(name, rules):
    name = name.lower()
    return sorted({str(rule["id"]) for rule in rules
                   if any(str(keyword).lower() in name for keyword in rule.get("searchKeyword", []))})


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


@checkpointed
def report(store, client, *, rules_dir=None, **_):
    client.check()
    if rules_dir is None:
        raise ValueError("report requires --rules-dir")
    from pathlib import Path
    rules_path = Path(rules_dir) / "rules/en/general.json"
    if not rules_path.is_file():
        raise ValueError(f"Published rules not found: {rules_path}")
    rules = read_json(rules_path, [])
    overrides = store.config("sdk-overrides.yaml").get("groups", {})
    totals, groups = Counter(), {}
    for path in sorted((store.root / "data/artifacts").glob("*/*.json")):
        client.check()
        artifact = read_json(path, None)
        label = group_label(artifact["coordinate"].split(":")[0], overrides)
        result = groups.setdefault(label, {"total": 0, "covered": 0, "prefixes": Counter(), "rules": set()})
        totals["artifacts"] += 1
        # Attribute variants of one component are counted once per artifact.
        components = sorted({(item["name"], item["type"]) for item in artifact["components"]})
        totals["with_components"] += bool(components)
        totals["components"] += len(components)
        for name, _ in components:
            matches = matching_rules(name, rules)
            result["total"] += 1
            result["covered"] += bool(matches)
            result["rules"].update(matches)
            if not matches:
                result["prefixes"][".".join(name.split(".")[:-1][:3]) or name] += 1
    caps = Counter(cap for repository in store.repositories for cap in repository["capabilities"])
    lines = ["# Component coverage", "", f"Repositories: {len(store.repositories)}", "",
             "| Capability | Repositories |", "| --- | ---: |"]
    lines.extend(f"| {cap} | {caps[cap]} |" for cap in sorted(caps))
    lines.extend(["", f"Artifacts: {totals['artifacts']}", f"Artifacts with components: {totals['with_components']}",
                  f"Components: {totals['components']}", "", "Components are unique (name, type) pairs per artifact, across fetched versions.", "",
                  "| SDK group | Total | Covered | Missed | Top missed package prefixes | Matching rule ids |",
                  "| --- | ---: | ---: | ---: | --- | --- |"])
    for label, value in sorted(groups.items(), key=lambda item: (-(item[1]["total"] - item[1]["covered"]), item[0])):
        prefixes = ", ".join(f"{prefix} ({count})" for prefix, count in sorted(value["prefixes"].items(), key=lambda item: (-item[1], item[0]))[:10])
        lines.append(f"| {_cell(label)} | {value['total']} | {value['covered']} | {value['total'] - value['covered']} | {_cell(prefixes)} | {_cell(', '.join(sorted(value['rules'])))} |")
    lines.extend(["", "## Repositories without enumeration or seeds", ""])
    unseeded = []
    coordinates = store.state.get("coordinates", {})
    for repository in store.repositories:
        url, caps = repository["url"], repository["capabilities"]
        if caps != ["metadata"]:
            continue
        has_seeds = coordinates.get("*", []) or coordinates.get(url, []) or any(
            artifact["repository"] == url for artifact in store.state.get("artifacts", {}).values())
        if not has_seeds:
            unseeded.append(url)
    lines.extend([f"- {url}" for url in sorted(unseeded)] or ["None."])
    path = store.root / "reports/coverage.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    store.save()
    return True
