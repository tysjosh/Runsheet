#!/usr/bin/env python3
"""Export / restore the Elasticsearch indices that Postgres cannot rebuild.

Most Elasticsearch content here is a **projection**: it is fed by the
transactional outbox and can be regenerated with
``python -m persistence.rebuild_from_postgres --all``. Three indices are not.

``persistence.rebuild_from_postgres.ES_ONLY_INDICES`` names them, and a unit test
asserts each entry genuinely has no projector, no rebuild spec and no ORM model —
so the list cannot rot into scaremongering about something since fixed. Losing the
Elasticsearch cluster loses this data outright, and
``truck_compartments.last_loaded_product`` is what the cross-contamination guard
reads before assigning a product to a compartment: without it the guard cannot
tell that a compartment last carried diesel before loading gasoline.

A proper answer is either a Postgres source of truth for these entities (a
migration plus ORM models, projectors and repository rewiring) or a configured
Elasticsearch snapshot repository. Both need decisions and infrastructure this
script deliberately does not assume. What it provides is a portable
export/restore that works against any cluster with no extra setup, so the gap is
covered while that decision is made.

The index list is imported, never restated, so adding a fourth ES-only index
extends this backup automatically.

Usage
-----
    ENVIRONMENT=production python -m scripts.es_only_backup export --out-dir ./es-backup
    ENVIRONMENT=production python -m scripts.es_only_backup verify --out-dir ./es-backup
    ENVIRONMENT=production python -m scripts.es_only_backup restore --out-dir ./es-backup
    ENVIRONMENT=development python -m scripts.es_only_backup drill
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Dict, Iterator, List, Tuple

_SCROLL_SIZE = 500
_SCROLL_KEEPALIVE = "2m"


def _client():
    from services.elasticsearch_service import elasticsearch_service

    client = elasticsearch_service.client
    if client is None:
        raise SystemExit(
            "no Elasticsearch client — ENVIRONMENT=test skips the connection; "
            "run with development/staging/production"
        )
    return client


def _indices() -> Tuple[str, ...]:
    from persistence.rebuild_from_postgres import ES_ONLY_INDICES

    return ES_ONLY_INDICES


def _scroll(client, index: str) -> Iterator[Tuple[str, dict]]:
    """Yield ``(_id, _source)`` for every document in ``index``."""
    resp = client.search(
        index=index, query={"match_all": {}}, size=_SCROLL_SIZE,
        scroll=_SCROLL_KEEPALIVE,
    )
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]
    try:
        while hits:
            for hit in hits:
                yield hit["_id"], hit["_source"]
            resp = client.scroll(scroll_id=scroll_id, scroll=_SCROLL_KEEPALIVE)
            scroll_id = resp.get("_scroll_id")
            hits = resp["hits"]["hits"]
    finally:
        if scroll_id:
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass


def cmd_export(out_dir: pathlib.Path) -> int:
    client = _client()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, int] = {}

    for index in _indices():
        path = out_dir / f"{index}.ndjson"
        if not client.indices.exists(index=index):
            print(f"  {index}: absent — nothing to export")
            manifest[index] = 0
            path.write_text("", encoding="utf-8")
            continue
        n = 0
        with path.open("w", encoding="utf-8") as handle:
            for doc_id, source in _scroll(client, index):
                # id is carried alongside the document so a restore reproduces
                # the same ids: truck_compartments keys on truck_id_compartment_id
                # and the app looks documents up by that id, not by a query.
                handle.write(json.dumps({"_id": doc_id, "_source": source}) + "\n")
                n += 1
        manifest[index] = n
        print(f"  {index}: exported {n} document(s) -> {path.name}")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    total = sum(manifest.values())
    print(f"wrote {out_dir}/manifest.json ({total} document(s) total)")
    if total == 0:
        print(
            "⚠️  every ES-only index was empty. That is either a fresh "
            "environment or a sign the data is already gone — check before "
            "treating this as a backup."
        )
    return 0


def cmd_verify(out_dir: pathlib.Path) -> int:
    """Check the export is internally consistent before trusting it."""
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"❌ {manifest_path} missing — this is not an export directory")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    failed = False
    for index in _indices():
        if index not in manifest:
            print(f"❌ {index} is not in the manifest — the export predates it")
            failed = True
            continue
        path = out_dir / f"{index}.ndjson"
        if not path.is_file():
            print(f"❌ {path.name} missing")
            failed = True
            continue
        lines = 0
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"❌ {path.name}:{lineno} is not valid JSON: {exc}")
                failed = True
                break
            if "_id" not in record or "_source" not in record:
                print(f"❌ {path.name}:{lineno} lacks _id/_source")
                failed = True
                break
            lines += 1
        else:
            if lines != manifest[index]:
                print(
                    f"❌ {path.name} has {lines} document(s) but the manifest "
                    f"claims {manifest[index]} — the export is truncated"
                )
                failed = True
            else:
                print(f"  {index}: {lines} document(s) match the manifest")
    if failed:
        return 1
    print("✅ export is internally consistent")
    return 0


def _restore_into(client, index: str, records: List[dict], target: str) -> int:
    from Agents.support.mvp_es_mappings import MVP_INDEX_MAPPINGS

    if not client.indices.exists(index=target):
        # Reuse the app's declared mapping when there is one, so a restored index
        # is not left with whatever dynamic mapping the first document implies —
        # the failure mode that turned tenant_id into analyzed text elsewhere.
        body = MVP_INDEX_MAPPINGS.get(index)
        if body is not None:
            client.indices.create(index=target, body=body)
        else:
            client.indices.create(index=target)

    actions: List[dict] = []
    for record in records:
        actions.append({"index": {"_index": target, "_id": record["_id"]}})
        actions.append(record["_source"])
    if actions:
        resp = client.bulk(operations=actions, refresh=True)
        if resp.get("errors"):
            first = next(
                (item for item in resp["items"] if list(item.values())[0].get("error")),
                None,
            )
            raise RuntimeError(f"bulk restore into {target} failed: {first}")
    return len(records)


def _read(out_dir: pathlib.Path, index: str) -> List[dict]:
    path = out_dir / f"{index}.ndjson"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def cmd_restore(out_dir: pathlib.Path) -> int:
    if cmd_verify(out_dir) != 0:
        print("refusing to restore an export that does not verify")
        return 1
    client = _client()
    for index in _indices():
        records = _read(out_dir, index)
        n = _restore_into(client, index, records, index)
        print(f"  {index}: restored {n} document(s)")
    return 0


def cmd_drill() -> int:
    """Export, restore into scratch indices, compare counts, clean up.

    The same reasoning as the Postgres restore drill: a backup nobody has
    restored is a guess.
    """
    import tempfile

    client = _client()
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = pathlib.Path(tmp)
        print("=== 1. export ===")
        cmd_export(out_dir)
        print("=== 2. verify ===")
        if cmd_verify(out_dir) != 0:
            return 1

        manifest = json.loads((out_dir / "manifest.json").read_text())
        total = sum(manifest.values())
        if total == 0:
            print(
                "❌ every ES-only index is empty, so this drill would compare "
                "zero to zero and prove nothing about the restore path"
            )
            return 1

        print("=== 3. restore into scratch indices ===")
        scratch: List[str] = []
        ok = True
        try:
            for index in _indices():
                target = f"{index}__drill"
                if client.indices.exists(index=target):
                    client.indices.delete(index=target)
                scratch.append(target)
                _restore_into(client, index, _read(out_dir, index), target)

            print("=== 4. compare document counts AND content ===")
            for index in _indices():
                target = f"{index}__drill"
                client.indices.refresh(index=target)
                got = client.count(index=target)["count"]
                want = manifest[index]
                if got != want:
                    print(f"  {index:<22} exported={want:<5} restored={got:<5} "
                          f"COUNT MISMATCH")
                    ok = False
                    continue

                # Counts alone would pass a restore that silently dropped fields
                # — which is the failure that matters here, because
                # truck_compartments.last_loaded_product is what the
                # cross-contamination guard reads. Compare the documents.
                exported = {r["_id"]: r["_source"] for r in _read(out_dir, index)}
                restored = {
                    doc_id: source for doc_id, source in _scroll(client, target)
                }
                if exported == restored:
                    print(f"  {index:<22} {got:<5} document(s), content identical")
                    continue

                ok = False
                missing_ids = sorted(set(exported) - set(restored))
                extra_ids = sorted(set(restored) - set(exported))
                differing = sorted(
                    doc_id for doc_id in set(exported) & set(restored)
                    if exported[doc_id] != restored[doc_id]
                )
                print(f"  {index:<22} CONTENT MISMATCH")
                if missing_ids:
                    print(f"      not restored: {missing_ids[:5]}")
                if extra_ids:
                    print(f"      unexpected:   {extra_ids[:5]}")
                for doc_id in differing[:3]:
                    lost = sorted(
                        set(exported[doc_id]) - set(restored[doc_id])
                    )
                    changed = sorted(
                        k for k in set(exported[doc_id]) & set(restored[doc_id])
                        if exported[doc_id][k] != restored[doc_id][k]
                    )
                    print(f"      {doc_id}: fields lost={lost} changed={changed}")
        finally:
            print("=== 5. drop scratch indices ===")
            for target in scratch:
                try:
                    client.indices.delete(index=target)
                except Exception:
                    pass

    if ok:
        print(f"✅ restore drill passed — {total} document(s) round-tripped")
        return 0
    print("❌ restore drill failed")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("export", "verify", "restore"):
        p = sub.add_parser(name)
        p.add_argument("--out-dir", type=pathlib.Path, required=True)
    sub.add_parser("drill")
    args = ap.parse_args()

    if args.command == "export":
        return cmd_export(args.out_dir)
    if args.command == "verify":
        return cmd_verify(args.out_dir)
    if args.command == "restore":
        return cmd_restore(args.out_dir)
    return cmd_drill()


if __name__ == "__main__":
    sys.exit(main())
