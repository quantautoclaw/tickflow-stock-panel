#!/usr/bin/env python3
"""Audit and optionally repair QuantX standard daily-bar units.

QuantX's canonical contract is volume=lots and amount=yuan. Older standard
snapshots can contain shares/yuan or lots/thousand-yuan rows. This script uses
QuantX's public ``repair_mixed_bar_units`` helper, creates a byte-for-byte
backup before every changed file, and replaces parquet files atomically.

Dry run (default):
    python repair_quantx_bar_units.py --data-root /path/to/quantdata

Apply:
    python repair_quantx_bar_units.py --data-root /path/to/quantdata --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import polars as pl
from quantx_data.bar_units import audit_mixed_bar_units, repair_mixed_bar_units


def _parquet_files(data_root: Path) -> list[Path]:
    table = data_root / "standard" / "std_bars_1d"
    if not table.is_dir():
        raise FileNotFoundError(f"QuantX standard daily table not found: {table}")
    return sorted(path for path in table.rglob("*.parquet") if path.is_file())


def _atomic_write(frame: pl.DataFrame, target: Path) -> None:
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    files = _parquet_files(data_root)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = (
        args.backup_dir.expanduser().resolve()
        if args.backup_dir
        else data_root / "backups" / f"std_bars_1d-units-{timestamp}"
    )
    table = data_root / "standard" / "std_bars_1d"

    totals = {
        "files": len(files),
        "rows": 0,
        "files_needing_repair": 0,
        "rows_needing_repair": 0,
        "files_repaired": 0,
    }
    details: list[dict] = []

    for path in files:
        frame = pl.read_parquet(path)
        before = audit_mixed_bar_units(frame)
        totals["rows"] += frame.height
        needs_repair = before.repaired_rows > 0
        if needs_repair:
            totals["files_needing_repair"] += 1
            totals["rows_needing_repair"] += before.repaired_rows

        item = {
            "file": str(path.relative_to(table)),
            "rows": frame.height,
            "before": before.as_dict(),
            "repaired": False,
        }
        if args.apply and needs_repair:
            repaired, _ = repair_mixed_bar_units(frame)
            after = audit_mixed_bar_units(repaired)
            if after.repaired_rows:
                raise RuntimeError(
                    f"unit repair validation failed for {path}: {after.as_dict()}"
                )
            backup = backup_dir / path.relative_to(table)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            _atomic_write(repaired, path)
            item["after"] = after.as_dict()
            item["backup"] = str(backup)
            item["repaired"] = True
            totals["files_repaired"] += 1
        details.append(item)
        print(json.dumps(item, ensure_ascii=False))

    report = {
        "data_root": str(data_root),
        "apply": args.apply,
        "backup_dir": str(backup_dir) if args.apply else None,
        "totals": totals,
        "files": details,
    }
    report_path = (
        backup_dir / "repair-report.json"
        if args.apply
        else data_root / "standard" / "std_bars_1d-unit-audit.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), **totals}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
