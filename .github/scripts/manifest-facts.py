#!/usr/bin/env python3
"""Reads `aapt2 dump xmltree --file AndroidManifest.xml` output on stdin and prints facts that
decide whether Android's installer accepts the package. One `key=value` per line."""
import re
import sys

COMPONENTS = {"activity", "activity-alias", "service", "receiver", "provider"}
stack = []  # (indent, element dict)
elements = []
for raw in sys.stdin:
    line = raw.rstrip("\n")
    m = re.match(r"^(\s*)E: (\S+)", line)
    if m:
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        el = {"tag": m.group(2), "attrs": {}, "children": [], "parent": stack[-1][1] if stack else None}
        if stack:
            stack[-1][1]["children"].append(el)
        elements.append(el)
        stack.append((indent, el))
        continue
    m = re.match(r"^\s*A: (?:http://schemas\.android\.com/apk/res/android:)?([A-Za-z]+)(?:\(0x[0-9a-f]+\))?=(.*)$", line)
    if m and stack:
        value = m.group(2)
        quoted = re.match(r'^"([^"]*)"', value)
        stack[-1][1]["attrs"][m.group(1)] = quoted.group(1) if quoted else value.split(" ")[0]

def name(el):
    return el["attrs"].get("name", "?")

components = [e for e in elements if e["tag"] in COMPONENTS and e["parent"] is not None and e["parent"]["tag"] == "application"]
missing = [f'{e["tag"]}:{name(e)}' for e in components
           if any(c["tag"] == "intent-filter" for c in e["children"]) and "exported" not in e["attrs"]]
names = [name(e) for e in components]
duplicates = sorted({n for n in names if names.count(n) > 1})
permissions = [name(e) for e in elements if e["tag"] == "permission"]
authorities = [e["attrs"].get("authorities", "?") for e in components if e["tag"] == "provider"]
uses = [name(e) for e in elements if e["tag"] in ("uses-permission", "uses-permission-sdk-23")]
dup_uses = sorted({n for n in uses if uses.count(n) > 1})
app = next((e for e in elements if e["tag"] == "application"), {"attrs": {}})

print("components=%d" % len(components))
print("missingExported=%s" % (",".join(missing) or "none"))
print("duplicateComponents=%s" % (",".join(duplicates) or "none"))
print("definesPermissions=%s" % (",".join(permissions) or "none"))
print("providerAuthorities=%s" % (",".join(authorities) or "none"))
print("usesPermissionCount=%d" % len(uses))
print("duplicateUsesPermission=%s" % (",".join(dup_uses) or "none"))
print("extractNativeLibs=%s" % app["attrs"].get("extractNativeLibs", "unset"))
print("applicationClass=%s" % app["attrs"].get("name", "unset"))
