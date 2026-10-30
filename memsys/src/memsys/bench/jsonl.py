"""评测输出用的 JSONL 读写。

进程可能在写到一半时被杀掉，留下一行残缺的记录。读的时候跳过坏行，写的时候先补上换行，
保证续跑不会因为一行残缺的记录而整体失败，也不会把下一条记录粘到残行上。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    """追加一行。调用方负责加锁。"""
    with open(path, "a+b") as fh:
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
        fh.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
