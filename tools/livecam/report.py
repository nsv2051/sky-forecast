#!/usr/bin/env python3
"""汇总 livecam JSONL 台账，输出每个机位的预测/实况对齐表。

仅读取 <out>/ledger/*.jsonl 并打印到标准输出，不写任何文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "data" / "livecam"

STATUS_ORDER = ("俯瞰云海", "云中雾中", "云层在上方", "晴空无云")
NO_PREDICTION = "无预测"
OVERLOOK_STATUS = "俯瞰云海"
SUSPICIOUS_DARK_FRAC = 0.55
SUSPICIOUS_EDGE_FRAC = 0.04


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="汇总 livecam 台账中的预测与实况代理指标。"
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="采集输出根目录（默认: %(default)s）",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="只统计该日期（含）之后的记录，格式 YYYY-MM-DD",
    )
    return parser


def _on_or_after(record: dict[str, Any], since: str) -> bool:
    stamp = (
        record.get("hour_local")
        or record.get("ts_local")
        or record.get("ts_utc")
        or ""
    )
    return str(stamp)[:10] >= since


def _load_records(out_dir: Path, since: str | None) -> list[dict[str, Any]]:
    ledger_dir = out_dir / "ledger"
    if not ledger_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError as exc:
            print("警告: 无法读取 %s: %s" % (path, exc), file=sys.stderr)
            continue
        with handle:
            for line_number, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        "警告: 跳过无效 JSONL %s:%d"
                        % (path, line_number),
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                if since and not _on_or_after(record, since):
                    continue
                records.append(record)
    return records


def _frame_ok(record: dict[str, Any]) -> bool:
    frame = record.get("frame")
    return bool(isinstance(frame, dict) and frame.get("ok"))


def _status(record: dict[str, Any]) -> str:
    prediction = record.get("prediction")
    if isinstance(prediction, dict):
        status = prediction.get("status")
        if status:
            return str(status)
    return NO_PREDICTION


def _suspicious(record: dict[str, Any]) -> bool:
    if _status(record) != OVERLOOK_STATUS:
        return False
    frame = record.get("frame")
    if not isinstance(frame, dict) or not frame.get("ok"):
        return False
    try:
        dark_frac = float(frame.get("dark_frac"))
        edge_frac = float(frame.get("edge_frac"))
    except (TypeError, ValueError):
        return False
    return (
        dark_frac >= SUSPICIOUS_DARK_FRAC
        or edge_frac < SUSPICIOUS_EDGE_FRAC
    )


def _distribution(records: list[dict[str, Any]]) -> str:
    counts = Counter(_status(record) for record in records)
    ordered = [status for status in STATUS_ORDER if counts.get(status)]
    ordered.extend(
        sorted(status for status in counts if status not in STATUS_ORDER)
    )
    if not ordered:
        return "-"
    return ", ".join("%s=%d" % (status, counts[status]) for status in ordered)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since 必须是 YYYY-MM-DD 格式")

    out_dir = Path(args.out).expanduser().resolve()
    records = _load_records(out_dir, args.since)
    if not records:
        print("台账为空: %s" % (out_dir / "ledger"))
        return 0

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for record in records:
        key = (
            str(record.get("room_id", "")),
            str(record.get("cam_name") or ""),
        )
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(record)

    print(
        "机位\t样本\t帧成功率\t预测状态分布\t可疑组合"
    )
    total_suspicious = 0
    total_frame_ok = 0
    for room_id, cam_name in order:
        cam_records = grouped[(room_id, cam_name)]
        samples = len(cam_records)
        frame_ok = sum(1 for record in cam_records if _frame_ok(record))
        total_frame_ok += frame_ok
        suspicious = sum(
            1 for record in cam_records if _suspicious(record)
        )
        total_suspicious += suspicious
        rate = 100.0 * frame_ok / samples if samples else 0.0
        label = "%s (%s)" % (cam_name or "(未命名)", room_id)
        print(
            "%s\t%d\t%.1f%%\t%s\t%d"
            % (
                label,
                samples,
                rate,
                _distribution(cam_records),
                suspicious,
            )
        )

    rate = 100.0 * total_frame_ok / len(records) if records else 0.0
    print(
        "合计\t%d\t%.1f%%\t-\t%d"
        % (len(records), rate, total_suspicious)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
