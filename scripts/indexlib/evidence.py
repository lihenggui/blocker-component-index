"""Manifest extraction and the exact, deterministic evidence schema."""

import json
from defusedxml import ElementTree as ET
from .versions import ordered

ANDROID = "{http://schemas.android.com/apk/res/android}"
ATTRIBUTES = ("name", "type", "exported", "process", "permission", "authorities", "actions", "categories", "metaData")


def parse_manifest(raw):
    root = ET.fromstring(raw, forbid_dtd=True)
    package = root.get("package", "")
    result = []
    for application in root.findall("application"):
        for node in application:
            kind = "activity" if node.tag == "activity-alias" else node.tag
            if kind not in ("activity", "service", "receiver", "provider"):
                continue
            name = node.get(ANDROID + "name")
            if not name:
                continue
            if "${" not in name:
                if name.startswith("."):
                    name = package + name
                elif "." not in name and package:
                    name = package + "." + name
            exported = node.get(ANDROID + "exported")
            result.append({
                "name": name,
                "type": kind,
                "exported": {"true": True, "false": False}.get(exported),
                "process": node.get(ANDROID + "process"),
                "permission": node.get(ANDROID + "permission"),
                "authorities": node.get(ANDROID + "authorities") if kind == "provider" else None,
                "actions": sorted({item.get(ANDROID + "name") for item in node.findall("intent-filter/action") if item.get(ANDROID + "name")}),
                "categories": sorted({item.get(ANDROID + "name") for item in node.findall("intent-filter/category") if item.get(ANDROID + "name")}),
                "metaData": sorted({item.get(ANDROID + "name") for item in node.findall("meta-data") if item.get(ANDROID + "name")}),
            })
    return sorted(result, key=lambda item: (item["name"], item["type"], signature(item)))


def signature(component):
    return json.dumps({key: component[key] for key in ATTRIBUTES}, ensure_ascii=False, sort_keys=True)


def components_at(evidence, version):
    versions = ordered(evidence["versions"])
    index = versions.index(version)
    return [component for component in evidence["components"] if any(
        versions.index(first) <= index <= versions.index(last)
        for first, last in component["versionRanges"]
    )]


def merge(evidence, coordinate, repository, version, fetched_at, sha256, dependencies, components):
    evidence = evidence or {"coordinate": coordinate, "repository": repository, "versions": {}, "components": []}
    snapshots = {v: components_at(evidence, v) for v in evidence["versions"]}
    snapshots[version] = components
    versions = dict(evidence["versions"])
    versions[version] = {"fetchedAt": fetched_at, "sha256": sha256, "dependencies": sorted(set(dependencies))}
    order = ordered(versions)
    variants = {}
    for v in order:
        for component in snapshots[v]:
            key = signature(component)
            if key not in variants:
                variants[key] = ({attr: component[attr] for attr in ATTRIBUTES}, set())
            variants[key][1].add(v)
    merged = []
    for component, present in variants.values():
        ranges = []
        for i, v in enumerate(order):
            if v in present:
                if i and order[i - 1] in present:
                    ranges[-1][1] = v
                else:
                    ranges.append([v, v])
        merged.append({"name": component["name"], "type": component["type"], "versionRanges": ranges,
                       **{key: component[key] for key in ATTRIBUTES[2:]}})
    merged.sort(key=lambda item: (item["name"], item["type"], signature(item)))
    return {"coordinate": coordinate, "repository": repository,
            "versions": {v: versions[v] for v in order}, "components": merged}
