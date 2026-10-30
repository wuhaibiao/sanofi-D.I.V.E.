"""按依赖顺序验收记忆系统，记录源码指纹、每阶段日志和未完成项。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from memsys.bench.provenance import fingerprint, prompt_fingerprint, source_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--data", default="data/longmemeval_oracle.json")
    parser.add_argument("--semantic", action="store_true", help="同时运行真实 embedding 检查")
    parser.add_argument("--with-models", action="store_true", help="检查模型并运行真实回答及机制回归")
    parser.add_argument("--full", action="store_true", help="前置检查通过后运行全量 Oracle 与本地评分")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--system-gaps", action="store_true", help="加入600轮持续运行、专项及加速验收")
    parser.add_argument("--extended-python", help="安装了 strict / fast 可选依赖的 Python 运行时")
    args = parser.parse_args()
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=True)
    data = str(Path(args.data).resolve())
    config = str(Path(args.config).resolve())
    provenance = source_manifest()
    workflow_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    inputs = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
              for name, path in (("config_sha256", config), ("data_sha256", data))}
    inputs["validation_sha256"] = fingerprint({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("scripts", "tests") for path in sorted((ROOT / folder).glob("*.py"))})
    manifest = output / "workflow.json"
    if manifest.exists():
        old = json.loads(manifest.read_text())
        if (old.get("provenance", {}).get("source_sha256") != provenance["source_sha256"]
                or old.get("workflow_sha256") != workflow_sha or old.get("inputs") != inputs):
            raise SystemExit("源码、流程或输入已变化，请使用新的验收输出目录；保留旧结果供对照。")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "provenance": provenance,
              "prompt_fingerprint": prompt_fingerprint(), "stages": [],
              "scope": "memory mechanisms and validation; excludes defense and participation",
              "status": "running", "experimental_scores_only": True}
    report["workflow_sha256"], report["inputs"] = workflow_sha, inputs
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    python = sys.executable
    memsys = [python, "-m", "memsys"]
    stages = [
        ("tests", [python, "-m", "unittest", "discover", "-s", "tests", "-q"], []),
        ("leakcheck", memsys + ["bench", "leakcheck", "--data", data], ["tests"]),
        ("write-coverage", [python, "scripts/check_write_coverage.py", "--data", data,
                            "--config", config, "--out", str(output / "write-coverage")], ["tests"]),
        ("lifecycle", [python, "scripts/check_lifecycle_pressure.py", "--out", str(output / "lifecycle")], ["tests"]),
        ("scale", memsys + ["bench", "scale", "--sizes", "100,1000,10000", "--queries", "5",
                            "--out", str(output / "scale.md")], ["tests"]),
    ]
    if args.system_gaps:
        stages.append(("soak", [python, "scripts/check_storage_soak.py", "--cycles", "600",
                                "--out", str(output / "soak")], ["tests"]))
        if args.extended_python:
            # 保留虚拟环境入口的符号链接；解析到基础解释器会丢失该环境的可选依赖。
            extended = str(Path(args.extended_python).expanduser().absolute())
            stages += [("extended-tests", [extended, "-m", "unittest", "discover", "-s", "tests", "-q"], ["tests"]),
                       ("acceleration", [extended, "scripts/check_vector_acceleration.py", "--out", str(output / "acceleration")],
                        ["extended-tests"]),
                       ("fast-scale", [extended, "-m", "memsys", "bench", "scale", "--sizes", "100,1000,10000",
                                        "--queries", "5", "--out", str(output / "fast-scale.md")], ["extended-tests"])]
        else:
            report["unrequested_optional_stages"] = ["local-tokenizer-runtime", "numpy-acceleration"]
    if args.semantic or args.with_models or args.full:
        stages.append(("semantic", [python, "scripts/check_semantic_retrieval.py", "--config", config,
                                    "--out", str(output / "semantic")], ["tests"]))
    if args.with_models or args.full:
        stages += [
            ("models", memsys + ["doctor", "--config", config, "--check"], ["tests", "leakcheck"]),
            ("behavior", memsys + ["bench", "behavior", "--config", config, "--out", str(output / "behavior")], ["models"]),
            ("mechanisms", memsys + ["bench", "mechanisms", "--config", config, "--out", str(output / "mechanisms")], ["models"]),
            ("paged-semantics", [python, "scripts/check_paged_semantics.py", "--config", config,
                                  "--out", str(output / "paged-semantics")], ["models"]),
            ("date-semantics", [python, "scripts/check_date_semantics.py", "--config", config,
                                 "--out", str(output / "date-semantics")], ["models"]),
        ]
    if args.full:
        engineering = ["soak"] if args.system_gaps else []
        if args.system_gaps and args.extended_python:
            engineering += ["extended-tests", "acceleration", "fast-scale"]
        stages += [
            ("oracle", memsys + ["bench", "run", "--config", config, "--data", data,
                                  "--out", str(output / "oracle"), "--workers", str(args.workers)],
             ["tests", "leakcheck", "write-coverage", "lifecycle", "scale", "semantic", "behavior", "mechanisms", "paged-semantics", "date-semantics"] + engineering),
            ("judge", memsys + ["bench", "judge", "--config", config, "--data", data,
                                 "--hyp", str(output / "oracle/hypotheses.jsonl"), "--workers", str(args.workers)], ["oracle"]),
            ("report", memsys + ["bench", "report", "--data", data, "--run", str(output / "oracle")], ["judge"]),
        ]
    statuses = {}
    for name, command, depends in stages:
        row = {"stage": name, "command": command, "depends_on": depends}
        unavailable = [d for d in depends if statuses.get(d) != "passed"]
        if unavailable:
            row.update(status="skipped", reason="prerequisites not passed: " + ", ".join(unavailable))
        else:
            print("Starting " + name, flush=True)
            started = time.monotonic()
            log = output / (name + ".log")
            with log.open("w") as stream:
                completed = subprocess.run(command, cwd=str(ROOT), env=env, stdout=stream, stderr=subprocess.STDOUT)
            row.update(status="passed" if completed.returncode == 0 else "blocked" if name == "models" else "failed",
                       exit_code=completed.returncode, seconds=round(time.monotonic() - started, 2), log=str(log))
            print(name + ": " + row["status"] + " (" + str(row["seconds"]) + " s)", flush=True)
        statuses[name] = row["status"]
        report["stages"].append(row)
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not args.full:
        report["unrequested_stages"] = ["oracle-500", "local-semantic-judge", "official-uniform-evaluation"]
    report["status"] = ("failed" if "failed" in statuses.values() else
                        "blocked" if "blocked" in statuses.values() or "skipped" in statuses.values() else "passed")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 记忆系统验收", "", "状态：" + report["status"], "",
             "| 阶段 | 状态 | 耗时 |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % (r["stage"], r["status"], str(r.get("seconds", "-"))) for r in report["stages"]]
    lines += ["", "本地评分和自拟回归只作为实验结果；正式成绩需要项目组统一复跑。",
              "输入覆盖、机械测试与真实 embedding 检查不能替代真实抽取及回答效果验证。",
              "日志、来源指纹和未执行原因见 workflow.json。"]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
