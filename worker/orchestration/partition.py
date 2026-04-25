"""Partition input for fan_out groups.

`resolve_partition_input` walks a dotted key into the job payload and returns the
addressed list (or [] when the key is missing / null / non-list). `split` divides the
resolved items into per-group index lists according to a strategy:

  - `chunks`: even split, with the first `n % group_count` groups taking one extra item.
  - `copies`: every group sees every index (broadcast).

Edge cases: when `group_count <= 0` or `n == 0`, returns N empty lists (where
N = max(group_count, 0)). Callers downstream rely on the "always returns
group_count lists" invariant so they can zip with `job_type.groups`.
"""
from __future__ import annotations


def resolve_partition_input(payload: dict, key: str) -> list:
    cur = payload
    for part in key.split('.'):
        if not isinstance(cur, dict):
            return []
        cur = cur.get(part)
        if cur is None:
            return []
    return cur if isinstance(cur, list) else []


def split(items: list, group_count: int, strategy: str) -> list[list[int]]:
    n = len(items)
    if group_count <= 0 or n == 0:
        return [[] for _ in range(max(group_count, 0))]
    if strategy == 'copies':
        return [list(range(n)) for _ in range(group_count)]
    # 'chunks'
    per, extra = divmod(n, group_count)
    out: list[list[int]] = []
    idx = 0
    for i in range(group_count):
        take = per + (1 if i < extra else 0)
        out.append(list(range(idx, idx + take)))
        idx += take
    return out
