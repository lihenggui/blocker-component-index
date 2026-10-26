"""Attribute package prefixes using component and artifact co-occurrence evidence."""

from collections import defaultdict
from .reporting import load_rules, matching_rules
from .storage import checkpointed, read_json, write_json


def base_prefix(name):
    package = name.split(".")[:-1]
    if "${" in name or len(package) < 2:
        return None
    return ".".join(package[:3])


@checkpointed
def prefixes(store, client, *, rules_dir=None, **_):
    client.check()
    rules = load_rules(rules_dir)
    bases, matches_by_name = {}, {}
    locations = defaultdict(lambda: defaultdict(set))
    for path in sorted((store.root / "data/artifacts").glob("*/*.json")):
        client.check()
        artifact = read_json(path, None)
        coordinate = artifact["coordinate"]
        group = coordinate.split(":")[0]
        for name in sorted({item["name"] for item in artifact["components"]}):
            prefix = base_prefix(name)
            if name not in matches_by_name:
                matches_by_name[name] = {int(rule) for rule in matching_rules(name, rules)}
            matches = matches_by_name[name]
            for rule in matches:
                locations[coordinate][rule].add(prefix)
            if prefix is None:
                continue
            value = bases.setdefault(prefix, {"names": set(), "artifacts": set(), "groups": set(), "rules": set()})
            value["names"].add(name)
            value["artifacts"].add(coordinate)
            value["groups"].add(group)
            value["rules"].update(matches)

    children = defaultdict(list)
    for prefix in sorted(bases):
        if prefix.count(".") == 2:
            children[prefix.rsplit(".", 1)[0]].append(prefix)
    collapsed = {}
    for parent, names in children.items():
        largest = max(names, key=lambda name: len(bases[name]["artifacts"]))
        same = sum(bases[name]["artifacts"] == bases[largest]["artifacts"] for name in names)
        if len(names) >= 5 and same * 5 >= len(names) * 4:
            collapsed.update((name, parent) for name in names)

    grouped = {}
    for prefix, value in bases.items():
        prefix = collapsed.get(prefix, prefix)
        result = grouped.setdefault(prefix, {key: set() for key in value})
        for key in value:
            result[key].update(value[key])

    rule_artifacts, exclusive = defaultdict(set), defaultdict(set)
    for coordinate, rules_at in locations.items():
        client.check()
        for rule, prefixes_at in rules_at.items():
            rule_artifacts[rule].add(coordinate)
            final = {collapsed.get(prefix, prefix) for prefix in prefixes_at}
            if len(final) == 1:
                exclusive[rule, next(iter(final))].add(coordinate)
            # Uncollapsed child buckets are still inside a two-segment parent.
            parents = {".".join(prefix.split(".")[:2]) if prefix is not None else None for prefix in final}
            if len(parents) == 1:
                exclusive[rule, next(iter(parents))].add(coordinate)

    result = []
    for prefix, value in grouped.items():
        client.check()
        artifacts = value["artifacts"]
        cooccurs = []
        for rule, declared in rule_artifacts.items():
            count = len(artifacts & (declared - exclusive[rule, prefix]))
            if count >= 2 and count * 5 >= len(artifacts) * 4:
                cooccurs.append({"rule": rule, "ratio": round(count / len(artifacts), 2), "artifacts": count})
        result.append({
            "prefix": prefix,
            "components": len(value["names"]),
            "groups": sorted(value["groups"]),
            "examples": sorted(value["names"])[:5],
            "coveredBy": sorted(value["rules"]),
            "cooccursWith": sorted(cooccurs, key=lambda item: (-item["ratio"], item["rule"])),
        })
    write_json(store.root / "data/prefixes.json", sorted(result, key=lambda item: (-item["components"], item["prefix"])))
    return True
