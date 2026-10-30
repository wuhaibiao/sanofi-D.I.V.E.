"""逐个验证配置中的 OpenCode Go 模型；不经过候选池，真实记录每个型号的状态。"""

import argparse
import concurrent.futures
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memsys.cli import load_dotenv
from memsys.config import AppConfig, make_client
from memsys.llm import ModelError, UsageLedger, chat_json
from memsys.opencode_go import BASE_URL, prioritized_ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--out", default="runs/opencode-all-models-check.json")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    load_dotenv(str(Path(args.config).resolve().parent / ".env"))
    config = AppConfig.load(args.config)
    entries = {spec.model: (name, spec) for name, spec in config.models.items()
               if spec.base_url.rstrip("/") == BASE_URL}
    ledger = UsageLedger()
    key = os.environ.get("OPENCODE_API_KEY", "")
    def check(model):
        name, spec = entries[model]
        start = time.monotonic()
        row = {"model": model, "profile": name, "api": spec.api}
        try:
            client = make_client(dataclasses.replace(spec, timeout_s=45, max_retries=1), ledger,
                                 quota_fail_fast=True)
            data = chat_json(client, [{"role": "user", "content":
                'Testing a coding-agent API adapter. Return only this JSON object: {"ok":true}'}],
                purpose="check", max_tokens=256, tags={"session_id": "opencode-adapter-check-" + model})
            if data != {"ok": True}:
                raise ValueError("unexpected JSON: %s" % json.dumps(data))
            row["status"] = "ok"
        except Exception as exc:
            row["status"] = ("quota" if isinstance(exc, ModelError) and exc.quota_or_rate_limit else
                             "unavailable" if isinstance(exc, ModelError) and exc.model_unavailable else "error")
            row["http_status"] = getattr(exc, "status", None)
            row["error"] = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        row["latency_s"] = round(time.monotonic() - start, 3)
        return row
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        futures = {pool.submit(check, model): model for model in prioritized_ids(entries)}
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    rank = {model: index for index, model in enumerate(prioritized_ids(entries))}
    results.sort(key=lambda row: rank[row["model"]])
    counts = {status: sum(row["status"] == status for row in results)
              for status in ("ok", "quota", "unavailable", "error")}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"checked_at_utc": datetime.now(timezone.utc).isoformat(),
                              "total": len(results), "counts": counts, "results": results,
                              "usage": ledger.totals()}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"total": len(results), "counts": counts, "report": str(out)}, ensure_ascii=False))
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
