"""比较精确向量加速与纯 Python 基准的分数、排序和耗时，不测模型效果。"""
from __future__ import annotations

import argparse
import json
import random
import time
from array import array
from pathlib import Path

from memsys import vecmath
from memsys.bench.provenance import source_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if vecmath._np is None:
        raise SystemExit("NumPy is missing; run with memsys[fast] installed")
    rng = random.Random(42)
    query = [rng.uniform(-1, 1) for _ in range(1024)]
    rows = []
    for size in (100, 1000, 10000):
        matrix = vecmath.VectorRows([array("f", (rng.uniform(-1, 1) for _ in range(1024))) for _ in range(size)])
        started = time.perf_counter()
        reference = vecmath.scores(query, matrix, "python")
        python_ms = 1000 * (time.perf_counter() - started)
        started = time.perf_counter()
        fast = vecmath.scores(query, matrix, "numpy")
        cold_ms = 1000 * (time.perf_counter() - started)
        samples = []
        for _ in range(10):
            started = time.perf_counter()
            vecmath.scores(query, matrix, "numpy")
            samples.append(1000 * (time.perf_counter() - started))
        error = max(abs(a-b) for a,b in zip(reference, fast))
        same_order = sorted(range(size), key=lambda i: (-reference[i], i)) == sorted(range(size), key=lambda i: (-fast[i], i))
        rows.append({"vectors": size, "dimensions": 1024, "max_absolute_error": error,
                     "same_order": same_order, "python_ms": round(python_ms, 3), "numpy_cold_ms": round(cold_ms, 3),
                     "numpy_warm_p95_ms": round(sorted(samples)[9], 3), "passed": same_order and error < 1e-10})
        print("Vectors %d: Python %.2f ms, NumPy warm P95 %.2f ms; same order %s" %
              (size, python_ms, rows[-1]["numpy_warm_p95_ms"], same_order), flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"mode": "exact scoring parity and timing; synthetic vectors; still a linear scan",
              "numpy_version": vecmath._np.__version__, "rows": rows, "passed": all(r["passed"] for r in rows),
              "provenance": source_manifest()}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
