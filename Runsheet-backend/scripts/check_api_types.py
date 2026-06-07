"""
Frontend/backend response-shape drift checker.

Compares the backend's OpenAPI response schemas (generated from the FastAPI
app's Pydantic models) against the frontend's hand-written TypeScript
interfaces, and reports the mismatch class that causes runtime crashes:

  RISK A — the frontend declares a field as required, but the backend can omit
           it (not in `required`) or send it as null. The UI then accesses a
           field that may be absent (e.g. `.map`, `.length`, `.toFixed`) and
           throws.
  RISK B — the frontend declares a required field the backend schema does not
           have at all (always `undefined` on real responses).

Matching is by identical type name (FE `interface X` == BE schema `X`), so it
produces some false positives where names collide (e.g. an envelope vs a model)
or where a type is a request body the FE legitimately requires. Treat the
output as a triage list, not a hard gate.

Usage (from Runsheet-backend/):
    ENVIRONMENT=test SKIP_MIGRATION_CHECK=1 ./venv/bin/python scripts/check_api_types.py
    # or against a saved spec:
    ./venv/bin/python scripts/check_api_types.py --openapi /path/to/openapi.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Resolve the frontend src dirs relative to this file so the script is
# location-independent (Runsheet-backend/scripts/ -> ../../runsheet/src/...).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
FE_DIRS = [
    os.path.join(_REPO, "runsheet", "src", "services"),
    os.path.join(_REPO, "runsheet", "src", "types"),
]


# ── Backend: OpenAPI component schemas ──────────────────────────────────────

def generate_openapi() -> dict:
    """Build the OpenAPI spec from the FastAPI app (no server/DB needed)."""
    sys.path.insert(0, _HERE + "/..")
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("SKIP_MIGRATION_CHECK", "1")
    from main import app  # noqa: E402  (import after env defaults)

    return app.openapi()


def prop_is_nullable(prop: dict) -> bool:
    t = prop.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in prop.get(key, []) or []:
            if sub.get("type") == "null":
                return True
    return False


def backend_schemas(spec: dict) -> dict:
    schemas = (spec.get("components") or {}).get("schemas") or {}
    out = {}
    for name, schema in schemas.items():
        props = schema.get("properties")
        if not props:
            continue
        required = set(schema.get("required", []) or [])
        out[name] = {
            fname: {
                "required": fname in required,
                "nullable": prop_is_nullable(fprop),
            }
            for fname, fprop in props.items()
        }
    return out


# ── Frontend: exported TS interfaces ────────────────────────────────────────

_INTERFACE_RE = re.compile(
    r"export\s+interface\s+([A-Za-z0-9_]+)\s*(?:extends[^{]+)?\{"
)
_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\?)?\s*:\s*(.+?);?$")


def _parse_fields(body: str) -> dict:
    fields, depth = {}, 0
    for raw in body.splitlines():
        line = raw.strip()
        opens = line.count("{") + line.count("[") + line.count("(")
        closes = line.count("}") + line.count("]") + line.count(")")
        if depth > 0:
            depth += opens - closes
            continue
        if line and line[0:2] not in ("//", "/*") and not line.startswith("*"):
            m = _FIELD_RE.match(line)
            if m:
                fname, optional, ftype = m.group(1), m.group(2), m.group(3)
                nullable = bool(re.search(r"(^|\W)null(\W|$)", ftype))
                fields[fname] = {
                    "optional": bool(optional),
                    "nullable": nullable,
                }
        depth += opens - closes
    return fields


def _parse_interfaces(text: str) -> dict:
    out = {}
    for m in _INTERFACE_RE.finditer(text):
        name, start, depth, j = m.group(1), m.end() - 1, 0, m.end() - 1
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out[name] = _parse_fields(text[start + 1 : j])
    return out


def frontend_interfaces() -> dict:
    out = {}
    for d in FE_DIRS:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith(".ts") and not fn.endswith(".test.ts"):
                    with open(os.path.join(root, fn)) as f:
                        for name, fields in _parse_interfaces(f.read()).items():
                            out.setdefault(name, fields)
    return out


# ── Compare ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--openapi", help="path to a saved openapi.json")
    args = ap.parse_args()

    spec = (
        json.load(open(args.openapi)) if args.openapi else generate_openapi()
    )
    be, fe = backend_schemas(spec), frontend_interfaces()
    common = sorted(set(be) & set(fe))
    print(
        f"backend schemas={len(be)}  frontend interfaces={len(fe)}  "
        f"matched-by-name={len(common)}\n"
    )

    risk_a, risk_b = [], []
    for name in common:
        for fname, fmeta in fe[name].items():
            fe_required = not fmeta["optional"] and not fmeta["nullable"]
            if not fe_required:
                continue
            if fname not in be[name]:
                risk_b.append((name, fname))
                continue
            bmeta = be[name][fname]
            if (not bmeta["required"]) or bmeta["nullable"]:
                why = "+".join(
                    w
                    for w, cond in (
                        ("optional", not bmeta["required"]),
                        ("nullable", bmeta["nullable"]),
                    )
                    if cond
                )
                risk_a.append((name, fname, why))

    def dump(rows, render):
        cur = None
        for row in rows:
            if row[0] != cur:
                print(f"\n  {row[0]}")
                cur = row[0]
            print(render(row))

    print("=== RISK A: FE required, BE optional/nullable ===")
    dump(risk_a, lambda r: f"      - {r[1]}  (BE: {r[2]})")
    if not risk_a:
        print("  (none)")
    print("\n=== RISK B: FE requires a field the BE schema lacks ===")
    dump(risk_b, lambda r: f"      - {r[1]}")
    if not risk_b:
        print("  (none)")
    print(
        f"\nSUMMARY: {len(risk_a)} required-vs-nullable, {len(risk_b)} FE-only "
        f"required fields across {len(common)} matched models."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
