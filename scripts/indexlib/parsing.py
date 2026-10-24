"""Generic Gradle, catalog, Maven metadata, and POM parsing."""

import ipaddress
import re
import tomllib
from urllib.parse import urlsplit, urlunsplit
from defusedxml import ElementTree as ET
from .storage import valid_coordinate
from .versions import ordered

BUILTINS = ("https://dl.google.com/android/maven2", "https://repo1.maven.org/maven2")
BUILTIN_HOSTS = {"dl.google.com", "maven.google.com", "repo1.maven.org", "repo.maven.apache.org"}
FILES = ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts")


def normalize_repository(value, *, discovery=False):
    if "$" in value or any(c.isspace() for c in value):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local")) or host.isdigit():
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if host == "plugins.gradle.org" or (discovery and host in BUILTIN_HOSTS):
            return None
        netloc = host.lower() + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), parts.query, ""))
    except ValueError:
        return None


def gradle_repositories(text):
    candidates = []
    # Text-match fragments often start inside the maven block, at the url line.
    for match in re.finditer(r"\b(?:url|setUrl)\s*(?:=\s*)?(?:\(\s*)?(?:uri\s*\(\s*)?[\"'](https?://[^\"'\s]+)[\"']", text):
        candidates.append(match.group(1))
    for match in re.finditer(r"\bmaven\s*\(\s*(?:url\s*=\s*)?(?:uri\s*\(\s*)?[\"'](https?://[^\"'\s]+)[\"']", text):
        candidates.append(match.group(1))
    return sorted({url for value in candidates if (url := normalize_repository(value, discovery=True))})


def coordinates(text, *, catalog=False):
    result = set()
    for match in re.finditer(r"[\"']([A-Za-z0-9_][A-Za-z0-9_.+\-]*:[A-Za-z0-9_][A-Za-z0-9_.+\-]*)(?::[^\"'\s]+)?[\"']", text):
        result.add(match.group(1))
    # Map notation permits either key: value or key = value, in either order.
    notation = r"\b[A-Za-z_]\w*\s*(?:\(([^)]*)\)|((?:group|name)\s*[:=][^\n]+(?:,\s*\n\s*[^\n]+)*))"
    for match in re.finditer(notation, text):
        fields = dict(re.findall(r"\b(group|name)\s*[:=]\s*[\"']([^\"']+)[\"']", match.group(1) or match.group(2) or ""))
        if fields.get("group") and fields.get("name"):
            result.add(fields["group"] + ":" + fields["name"])
    if catalog:
        try:
            libraries = tomllib.loads(text).get("libraries", {})
        except tomllib.TOMLDecodeError:
            libraries = {}
        for value in libraries.values():
            if isinstance(value, str):
                result.add(":".join(value.split(":")[:2]))
            elif isinstance(value, dict):
                if "module" in value:
                    result.add(":".join(value["module"].split(":")[:2]))
                elif "group" in value and "name" in value:
                    result.add(value["group"] + ":" + value["name"])
    return sorted(value for value in result if valid_coordinate(value))


def xml(raw):
    root = ET.fromstring(raw, forbid_dtd=True)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return root


def metadata(raw):
    root = xml(raw)
    group, artifact = root.findtext("groupId"), root.findtext("artifactId")
    coordinate = f"{group}:{artifact}" if group and artifact else None
    versions = [node.text.strip() for node in root.findall("versioning/versions/version") if node.text]
    return coordinate, ordered(v for v in versions if v and "/" not in v and "\\" not in v and v not in (".", ".."))


def pom(raw):
    root = xml(raw)
    properties = {node.tag: node.text or "" for node in root.findall("properties/*")}
    for field in ("groupId", "artifactId", "version"):
        value = root.findtext(field) or root.findtext("parent/" + field) or ""
        properties["project." + field] = properties["pom." + field] = value
    for field in ("groupId", "artifactId", "version"):
        properties["project.parent." + field] = root.findtext("parent/" + field) or ""

    def resolve(value):
        for _ in range(10):
            replaced = re.sub(r"\$\{([^}]+)\}", lambda match: properties.get(match.group(1), match.group(0)), value or "")
            if replaced == value:
                break
            value = replaced
        return value

    managed = {}
    for node in root.findall("dependencyManagement/dependencies/dependency"):
        managed[(resolve(node.findtext("groupId")), resolve(node.findtext("artifactId")))] = resolve(node.findtext("version"))
    dependencies = set()
    for node in root.findall("dependencies/dependency"):
        if resolve(node.findtext("scope") or "compile") not in ("compile", "runtime"):
            continue
        if resolve(node.findtext("optional") or "false").lower() == "true":
            continue
        group, artifact = resolve(node.findtext("groupId")), resolve(node.findtext("artifactId"))
        version = resolve(node.findtext("version") or managed.get((group, artifact), ""))
        if valid_coordinate(group + ":" + artifact) and version and "${" not in version:
            dependencies.add(f"{group}:{artifact}:{version}")
    repositories = sorted({url for node in root.findall("repositories/repository/url")
                           if (url := normalize_repository(resolve(node.text or ""), discovery=True))})
    return resolve(root.findtext("packaging") or "jar"), sorted(dependencies), repositories


def module_info(raw):
    import json
    value = json.loads(raw)
    files, dependencies = set(), set()
    for variant in (value.get("variants") or []) if isinstance(value, dict) else []:
        if not isinstance(variant, dict) or not isinstance(variant.get("attributes") or {}, dict):
            continue
        if (variant.get("attributes") or {}).get("org.gradle.usage") not in (None, "java-api", "java-runtime", "kotlin-api", "kotlin-runtime"):
            continue
        # Vendor-published .module files may carry nulls; treat them as absent.
        aar_files = [item.get("url") or item.get("name") for item in variant.get("files") or []
                     if isinstance(item, dict) and (item.get("name") or item.get("url") or "").endswith(".aar")]
        if not aar_files:
            continue
        files.update(aar_files)
        for dependency in variant.get("dependencies") or []:
            if not isinstance(dependency, dict):
                continue
            version = dependency.get("version") or {}
            if isinstance(version, dict):
                version = version.get("requires") or version.get("strictly") or version.get("prefers")
            coordinate = (dependency.get("group") or "") + ":" + (dependency.get("module") or "")
            if valid_coordinate(coordinate) and version:
                dependencies.add(f"{coordinate}:{version}")
    return sorted(files), sorted(dependencies)
