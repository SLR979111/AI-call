#!/usr/bin/env python3
"""Validate AICall event fixtures against contracts/schemas.

Usage:
    python contracts/validate.py [fixtures_dir|file.jsonl ...]
    python contracts/validate.py contracts/fixtures --schema contracts/schemas/event.v1.json

Default target is contracts/fixtures; default schema is event.v1.json.
Each *.jsonl file is validated line by line (blank lines skipped); each *.json
file is validated as one document. Prints ``OK <file> <n>`` per clean file and
exits 1 on any error.

Uses jsonschema when installed. If it is missing, a built-in draft-07 subset
validator (type/required/properties/enum/const/oneOf/allOf/if-then/pattern/
minimum/minLength/items/$ref) takes over so the check stays runnable offline.
The builtin engine reports the first error per document; jsonschema reports all.
Neither engine asserts `format` (date-time) by default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

CONTRACTS_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = CONTRACTS_DIR / "schemas"
DEFAULT_SCHEMA = SCHEMAS_DIR / "event.v1.json"

try:  # preferred path
    import jsonschema  # type: ignore

    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - exercised when dependency absent
    jsonschema = None  # type: ignore
    HAVE_JSONSCHEMA = False


class ValidationError(ValueError):
    """Raised by the fallback validator."""


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, name: str) -> bool:
    if name == "integer":
        # JSON Schema counts integral floats (1.0) as integers.
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    expected = _TYPE_MAP.get(name)
    if expected is None:
        raise ValidationError(f"unsupported schema type {name!r}")
    return isinstance(value, expected)


def _resolve_ref(root: dict, ref: str) -> dict:
    if not ref.startswith("#"):
        raise ValidationError(f"external $ref not supported: {ref}")
    node: Any = root
    for raw in ref.lstrip("#/").split("/"):
        if not raw:
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        node = node[token]
    if not isinstance(node, dict):
        raise ValidationError(f"$ref does not target a schema: {ref}")
    return node


def _check(value: Any, schema: dict, root: dict, path: str) -> None:
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _check(value, _resolve_ref(root, schema["$ref"]), root, path)

    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else list(names)
        if not any(_type_ok(value, n) for n in names):
            raise ValidationError(f"{path or '$'}: expected type {names}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path or '$'}: {value!r} not in enum {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{path or '$'}: {value!r} != const {schema['const']!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValidationError(f"{path}: shorter than minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValidationError(f"{path}: {value!r} does not match {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path}: {value} > maximum {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValidationError(f"{path or '$'}: missing required property {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, root, f"{path}.{key}" if path else key)
        extra = schema.get("additionalProperties", True)
        if extra is not True:
            for key, val in value.items():
                if key in props:
                    continue
                if extra is False:
                    raise ValidationError(f"{path or '$'}: unexpected property {key!r}")
                _check(val, extra, root, f"{path}.{key}" if path else key)

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _check(item, items, root, f"{path}[{i}]")

    for sub in schema.get("allOf", []):
        _check(value, sub, root, path)

    if "oneOf" in schema:
        failures = []
        for i, sub in enumerate(schema["oneOf"]):
            try:
                _check(value, sub, root, path)
            except ValidationError as exc:
                failures.append(f"#{i}: {exc}")
        matched = len(schema["oneOf"]) - len(failures)
        if matched != 1:
            raise ValidationError(
                f"{path or '$'}: must match exactly one oneOf branch, matched {matched} ({'; '.join(failures)})"
            )

    if "if" in schema:
        try:
            _check(value, schema["if"], root, path)
            branch = schema.get("then")
        except ValidationError:
            branch = schema.get("else")
        if isinstance(branch, dict):
            _check(value, branch, root, path)


def validate(instance: Any, schema: dict) -> list[str]:
    """Return a list of human-readable errors (empty means valid)."""
    if HAVE_JSONSCHEMA:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
        return [f"{'/'.join(map(str, err.path)) or '$'}: {err.message}" for err in validator.iter_errors(instance)]
    try:
        _check(instance, schema, schema, "")
    except ValidationError as exc:
        return [str(exc)]
    return []


def load_schema(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    if not isinstance(schema, dict):
        raise SystemExit(f"ERROR: {path} is not a JSON object")
    return schema


def _seq_of(doc: Any) -> int | None:
    """Extract an integral envelope `seq` (supports 1 and 1.0), else None."""
    if not isinstance(doc, dict):
        return None
    seq = doc.get("seq")
    if isinstance(seq, bool):
        return None
    if isinstance(seq, int):
        return seq
    if isinstance(seq, float) and seq.is_integer():
        return int(seq)
    return None


def iter_documents(path: Path) -> Iterable[tuple[str, Any, str | None]]:
    """Yield (label, document, error) triples: one per JSONL line, or one per JSON file."""
    if path.suffix.lower() == ".json":
        try:
            with path.open("r", encoding="utf-8") as fh:
                yield path.name, json.load(fh), None
        except json.JSONDecodeError as exc:
            yield path.name, None, f"invalid JSON: {exc}"
        return
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            label = f"{path.name}:{lineno}"
            try:
                yield label, json.loads(line), None
            except json.JSONDecodeError as exc:
                yield label, None, f"invalid JSON: {exc}"


def iter_targets(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    files = sorted(target.glob("*.jsonl")) + sorted(target.glob("*.json"))
    return [p for p in files if p.is_file()]


def display(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate *.jsonl event fixtures against a JSON Schema (default: event.v1.json)."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="PATH",
        help="fixtures directory, *.jsonl file or *.json file (default: contracts/fixtures)",
    )
    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA),
        metavar="PATH",
        help="schema to validate against (default: contracts/schemas/event.v1.json)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print failures and the final summary")
    args = parser.parse_args(argv)

    schema_path = Path(args.schema)
    if not schema_path.is_absolute():
        schema_path = Path.cwd() / schema_path
    schema_path = schema_path.resolve()
    if not schema_path.is_file():
        print(f"ERROR: schema not found: {schema_path}", file=sys.stderr)
        return 1
    try:
        schema = load_schema(schema_path)
    except (OSError, json.JSONDecodeError) as exc:
        print("ERROR: cannot load schema {}: {}".format(schema_path, exc), file=sys.stderr)
        return 1

    targets: list[Path] = []
    for raw in args.targets or [str(CONTRACTS_DIR / "fixtures")]:
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        found = iter_targets(path.resolve())
        if not found:
            print(f"ERROR: no .jsonl/.json files found at {path}", file=sys.stderr)
            return 1
        targets.extend(found)
    targets = list(dict.fromkeys(targets))
    if not targets:
        print("ERROR: nothing to validate", file=sys.stderr)
        return 1

    engine = "jsonschema" if HAVE_JSONSCHEMA else "builtin(draft-07 subset)"
    if not args.quiet:
        print(f"# schema: {display(schema_path)}")
        print(f"# engine: {engine}")

    failed = 0
    checked = 0
    for path in targets:
        errors: list[str] = []
        count = 0
        last_seq: int | None = None
        try:
            for label, doc, parse_error in iter_documents(path):
                count += 1
                checked += 1
                if parse_error:
                    errors.append(f"{label}: {parse_error}")
                    continue
                for err in validate(doc, schema):
                    errors.append(f"{label}: {err}")
                seq = _seq_of(doc)
                if seq is not None:
                    if last_seq is not None and seq <= last_seq:
                        errors.append(f"{label}: seq {seq} not strictly increasing (previous {last_seq})")
                    if last_seq is None and seq != 1:
                        errors.append(f"{label}: seq must start at 1, got {seq}")
                    last_seq = seq
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}: invalid JSON: {exc}")
        except OSError as exc:
            errors.append(f"{path.name}: cannot read: {exc}")

        if errors or count == 0:
            failed += 1
            print(f"FAIL {display(path)}")
            for err in errors:
                print(f"  - {err}")
            if count == 0:
                print("  - no documents found")
        elif not args.quiet:
            print(f"OK {display(path)} {count}")

    if failed:
        print(f"FAILED: {failed} file(s), {checked} document(s) checked (engine: {engine})", file=sys.stderr)
        return 1
    print(f"PASS: {len(targets)} file(s), {checked} document(s) valid (engine: {engine})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
