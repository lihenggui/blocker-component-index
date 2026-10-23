"""Maven-style numeric and qualifier comparison, without lexical version sorting."""

import re
from functools import total_ordering

QUALIFIERS = {name: i for i, name in enumerate(("alpha", "beta", "milestone", "rc", "snapshot", "", "sp"))}
ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}


def _tokens(value):
    matches = list(re.finditer(r"[.\-]|\d+|[^.\-\d]+", value.lower()))
    root = []
    stack = [root]

    def descend():
        child = []
        stack[-1].append(child)
        stack.append(child)

    previous = None
    for i, match in enumerate(matches):
        token = match.group()
        if token in (".", "-"):
            if previous is None or previous in (".", "-"):
                stack[-1].append(0)
            if token == "-":
                descend()
        elif token.isdigit():
            if previous is not None and previous not in (".", "-") and not previous.isdigit():
                descend()
            stack[-1].append(int(token))
        else:
            followed_by_digit = i + 1 < len(matches) and matches[i + 1].group().isdigit()
            if (previous is not None and previous.isdigit()) or ((followed_by_digit or i == len(matches) - 1) and stack[-1]):
                descend()
            if followed_by_digit:
                token = {"a": "alpha", "b": "beta", "m": "milestone"}.get(token, token)
            stack[-1].append(ALIASES.get(token, token))
        previous = match.group()
    for items in reversed(stack):
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if item == 0 or item == "" or item == []:
                del items[i]
            elif not isinstance(item, list):
                break
    return root


def _qualifier(value):
    return (QUALIFIERS[value], "") if value in QUALIFIERS else (7, value)


def _compare(left, right):
    if left is None:
        return -_compare(right, None) if right is not None else 0
    if right is None:
        if isinstance(left, list):
            return next((result for item in left if (result := _compare(item, None))), 0)
        right = 0 if isinstance(left, int) else ""
    if isinstance(left, list) and isinstance(right, list):
        for i in range(max(len(left), len(right))):
            result = _compare(left[i] if i < len(left) else None, right[i] if i < len(right) else None)
            if result:
                return result
        return 0
    if type(left) is not type(right):
        rank = {str: 0, list: 1, int: 2}
        return (rank[type(left)] > rank[type(right)]) - (rank[type(left)] < rank[type(right)])
    if isinstance(left, str):
        left, right = _qualifier(left), _qualifier(right)
    return (left > right) - (left < right)


@total_ordering
class MavenVersion:
    def __init__(self, value):
        self.value = value
        self.tokens = _tokens(value)

    def _compare(self, other):
        return _compare(self.tokens, other.tokens)

    def __eq__(self, other):
        return isinstance(other, MavenVersion) and self._compare(other) == 0

    def __lt__(self, other):
        return self._compare(other) < 0


def ordered(versions):
    # Equivalent Maven versions still have a stable order in JSON and ranges.
    return sorted(set(versions), key=lambda value: (MavenVersion(value), value))
