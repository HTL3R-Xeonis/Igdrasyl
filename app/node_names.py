"""
What a new node may be called. The name becomes an ESXi VM name (and its folder on the datastore), a GNS3 node name, the
device's host name, a key in the topology YAML, a part of URLs and of shell commands - so a name like "../etc", "a b",
"x/y" or one with a line break breaks something on the way, often only at deploy time. Names that already exist in a
project are not checked (an imported topology keeps working); only names typed in now.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_LENGTH = 63  # a host name label
_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def validate(name: str, existing: Iterable[str] = ()) -> None:
    """
    :raises ValueError: empty, too long, with characters other than letters, digits, '-' and '_' (it must start with a
        letter or digit), or - ignoring case, since ESXi treats R1 and r1 as one VM - already taken.
    """
    if not name.strip():
        raise ValueError("name must not be empty")
    if len(name) > MAX_LENGTH:
        raise ValueError(f"name must not be longer than {MAX_LENGTH} characters")
    if not _PATTERN.fullmatch(name):
        raise ValueError(
            "name may only contain letters, digits, '-' and '_' and must start with a letter or digit"
        )
    taken = {other.lower() for other in existing}
    if name.lower() in taken:
        raise ValueError(
            f"a node named '{name}' already exists (names are not case-sensitive)"
        )
