"""向量小工具。单题的记忆规模在几百条以内，纯 Python 暴力余弦就够快，不需要向量数据库。"""

from __future__ import annotations

import operator
import os
from typing import List, Sequence

try:
    import numpy as _np
except ImportError:
    _np = None


class VectorRows(list):
    """兼容原列表接口，矩阵缓存跟随 Store 的状态与事务缓存一起失效。"""
    dense = None


def backend_name():
    requested = os.environ.get("MEMSYS_VECTOR_BACKEND", "auto")
    if requested not in ("auto", "python", "numpy"):
        raise ValueError("MEMSYS_VECTOR_BACKEND must be auto, python or numpy")
    if requested == "numpy" and _np is None:
        raise ValueError("install memsys[fast] to use the numpy vector backend")
    return "numpy" if _np is not None and requested != "python" else "python"


def normalize(vec: Sequence[float]) -> List[float]:
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(map(operator.mul, a, b))


def scores(query: Sequence[float], matrix: Sequence[Sequence[float]], backend=None) -> List[float]:
    """query 和 matrix 每一行的点积；两边都已归一化时就是余弦相似度。"""
    if not matrix:
        return []
    if any(len(row) != len(query) for row in matrix):
        raise ValueError("query and memory embedding dimensions differ; reindex with a consistent model")
    selected = backend or backend_name()
    if selected == "numpy":
        if _np is None:
            raise ValueError("numpy backend is unavailable")
        dense = getattr(matrix, "dense", None)
        if dense is None:
            dense = _np.asarray(matrix, dtype=_np.float64)
            if isinstance(matrix, VectorRows):
                matrix.dense = dense
        return (dense @ _np.asarray(query, dtype=_np.float64)).tolist()
    if selected != "python":
        raise ValueError("unknown vector backend")
    return [dot(query, row) for row in matrix]
