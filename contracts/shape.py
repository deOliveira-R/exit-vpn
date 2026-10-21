"""Shape checks for the JSON contracts in this folder (stdlib only).

A sample file shows one example of a wire format. The producer's tests call `check()` on
what it really emits: every object must have exactly the sample's keys, and each value the
sample value's JSON type (numbers are one type; `null` on either side matches anything, so
optional fields stay optional). An object written as `{"*": <sample>}` is a map: any keys,
each value shaped like the sample. Lists are checked element by element against the
sample's first element.

`UPDATE_CONTRACTS=1` merges the producer's output into the sample instead (a deliberate
contract change, reviewed in the diff): new fields are added, hand-added fields stay, and a
field is removed only by editing the file. `maps` names the paths written as `{"*": …}`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).parent


def _kind(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def mismatches(actual, sample, path: str = "$", allow_missing: bool = False,
               optional: Iterable[str] = (), _pattern: str = "$") -> list[str]:
    """Where `actual` does not have the shape of `sample` (empty: it conforms).
    `allow_missing`: top-level keys of the sample may be absent (event payloads).
    `optional`: paths (like `$.slots[].meta`) of objects whose keys may each be absent."""
    optional = set(optional)
    if actual is None or sample is None:
        return []
    ka, ks = _kind(actual), _kind(sample)
    if ka != ks:
        return [f"{path}: {ka}, the sample has {ks}"]
    if ks == "array":
        if not sample:
            return []
        return [m for i, a in enumerate(actual)
                for m in mismatches(a, sample[0], f"{path}[{i}]", False, optional, _pattern + "[]")]
    if ks != "object":
        return []
    if list(sample) == ["*"]:
        return [m for k, v in actual.items()
                for m in mismatches(v, sample["*"], f"{path}.{k}", False, optional, _pattern + ".*")]
    may_miss = allow_missing or _pattern in optional
    out = [] if may_miss else [f"{path}: missing {k!r}" for k in sample if k not in actual]
    out += [f"{path}: unexpected {k!r} (add it to the sample)" for k in actual if k not in sample]
    for k in sample.keys() & actual.keys():
        out += mismatches(actual[k], sample[k], f"{path}.{k}", False, optional, f"{_pattern}.{k}")
    return out


class _Any:
    """While merging samples: values of different types were seen (written as null)."""


ANY = _Any()


def _finish(sample):
    if sample is ANY:
        return None
    if isinstance(sample, list):
        return [_finish(v) for v in sample]
    if isinstance(sample, dict):
        return {k: _finish(v) for k, v in sample.items()}
    return sample


def _as_sample(value, maps: set[str], path: str = "$"):
    if isinstance(value, list):   # one element: the union of all of them
        merged = None
        for v in value:
            merged = _merge(merged, _as_sample(v, maps, path + "[]"))
        return [merged] if value else []
    if isinstance(value, dict):
        if path in maps:
            merged = None
            for v in value.values():
                merged = _merge(merged, _as_sample(v, maps, path + ".*"))
            return {"*": merged}
        return {k: _as_sample(v, maps, f"{path}.{k}") for k, v in value.items()}
    return value


def _merge(old, new):
    """Union of two samples: keys from both, the non-null value where one is null, the old
    example value where both have one; values of different JSON types (a lever that is a
    number or a boolean) become null: any."""
    if old is ANY or new is ANY:
        return ANY
    if old is None:
        return new
    if new is None:
        return old
    if _kind(old) != _kind(new):
        return ANY
    if isinstance(old, list) and isinstance(new, list):   # one-element list samples
        if not old or not new:
            return old or new
        return [_merge(old[0], new[0])]
    if isinstance(old, dict) and isinstance(new, dict):
        if list(old) == ["*"] and list(new) == ["*"]:     # two samples of one map
            return {"*": _merge(old["*"], new["*"])}
        return {**old, **{k: _merge(old.get(k), v) for k, v in new.items()}}
    return old   # same type: keep the sample's example value (diffs show only shape changes)


def load(name: str):
    return json.loads((ROOT / f"{name}.json").read_text())


def check(name: str, actual, maps: Iterable[str] = (), optional: Iterable[str] = ()) -> None:
    """Assert that `actual` has the shape of `contracts/<name>.json` (or rewrite it)."""
    f = ROOT / f"{name}.json"
    if os.environ.get("UPDATE_CONTRACTS"):   # merged into the sample: hand-added fields stay
        f.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads(f.read_text()) if f.exists() else None
        new = _finish(_merge(old, _as_sample(actual, set(maps))))
        f.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        return
    problems = mismatches(actual, load(name), optional=optional)
    assert not problems, f"{name}.json contract:\n  " + "\n  ".join(problems)


def check_event(name: str, event: dict, maps: Iterable[str] = ()) -> None:
    """Events differ by kind: `contracts/<name>.json` maps each kind to one sample event.
    An event may leave out fields of its sample (payloads vary by where it is raised) but
    must not carry fields the sample lacks."""
    f = ROOT / f"{name}.json"
    samples = json.loads(f.read_text()) if f.exists() else {}
    kind = event["kind"]
    if os.environ.get("UPDATE_CONTRACTS"):
        samples[kind] = _finish(_merge(samples.get(kind), _as_sample(event, set(maps))))
        f.write_text(json.dumps(dict(sorted(samples.items())), indent=2, ensure_ascii=False) + "\n")
        return
    assert kind in samples, f"{name}.json has no sample for event kind {kind!r}"
    problems = mismatches(event, samples[kind], allow_missing=True)
    assert not problems, f"{name}.json [{kind}] contract:\n  " + "\n  ".join(problems)
