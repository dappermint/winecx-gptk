import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path


SCENARIOS = ("race", "idle_under_load", "serialization", "failure", "concurrency", "corruption", "disabled", "capacity", "import", "eviction", "idle_only", "concurrent_idle", "quiet", "age", "retry", "uncaptured_failure", "bypass_disabled", "destroy_bypass", "exit_race", "native_exit", "fast_exit", "exit_registration_failure", "exit_snapshot_failure", "invalid_timing", "quiet_active", "compile_failure", "destroy_flush", "late_recapture", "late_recapture_render", "shutdown_recapture", "shutdown_recapture_render")


def compiler_arguments(mesa, build, output):
    commands = json.loads((build / "compile_commands.json").read_text())
    entry = next(
        command
        for name in ("kk_metal_cache.c", "kk_pso_cache.c")
        for command in commands
        if Path(command["file"]).name == name
    )
    directory = Path(entry["directory"])
    source = Path(entry["file"])
    if not source.is_absolute():
        source = (directory / source).resolve()
    original_mesa = source.parents[3]
    arguments = entry.get("arguments") or shlex.split(entry["command"])
    result = []
    skip = False
    for argument in arguments:
        if skip:
            skip = False
            continue
        if argument in ("-o", "-MF", "-MT", "-MQ"):
            skip = True
            continue
        if argument in ("-c", "-MD", "-MMD"):
            continue
        if Path(argument).name in ("kk_metal_cache.c", "kk_pso_cache.c"):
            continue
        if argument.startswith("-I") and len(argument) > 2:
            include = Path(argument[2:])
            if not include.is_absolute():
                include = (directory / include).resolve()
            try:
                argument = "-I" + str(mesa / include.relative_to(original_mesa))
            except ValueError:
                pass
        result.append(argument)
    result.extend(
        [
            str(mesa / "src/kosmickrisp/vulkan/kk_metal_cache.c"),
            str(Path(__file__).with_name("kosmickrisp_metal_cache.c")),
            str(mesa / "src/c11/impl/threads_posix.c"),
            str(mesa / "src/c11/impl/time.c"),
            str(mesa / "src/util/simple_mtx.c"),
            str(mesa / "src/util/u_call_once.c"),
            str(mesa / "src/util/os_time.c"),
            "-include",
            str(Path(__file__).with_name("kosmickrisp_cache_hooks.h")),
            "-pthread",
            "-UNDEBUG",
            "-o",
            str(output),
        ]
    )
    return result, directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesa", required=True, type=Path)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("--out must be empty")
    out.mkdir(parents=True, exist_ok=True)
    binary = out / "metal-cache-regression"
    arguments, directory = compiler_arguments(
        args.mesa.resolve(), args.build.resolve(), binary
    )
    compile_result = subprocess.run(
        arguments, cwd=directory, capture_output=True, text=True
    )
    (out / "compile.stdout").write_text(compile_result.stdout)
    (out / "compile.stderr").write_text(compile_result.stderr)
    if compile_result.returncode:
        print(compile_result.stderr)
        return 1
    results = []
    for scenario in args.scenario or SCENARIOS:
        start = time.monotonic_ns()
        marker = out / (scenario + ".exit.json")
        event_log = out / (scenario + ".events.jsonl")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MESA_KK_PSO_CACHE") and k != "MESA_KK_CACHE_LOG"}
        env.update(KK_TEST_EXIT_MARKER=str(marker),
                   MESA_KK_CACHE_LOG=str(event_log))
        try:
            completed = subprocess.run(
                [str(binary), scenario],
                capture_output=True,
                text=True,
                timeout=args.timeout,
                env=env,
            )
            result = {
                "scenario": scenario,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except subprocess.TimeoutExpired as error:
            result = {
                "scenario": scenario,
                "returncode": None,
                "stdout": (error.stdout or b"").decode(errors="replace"),
                "stderr": (error.stderr or b"").decode(errors="replace"),
                "timeout": True,
            }
        if result["returncode"] == 0:
            if scenario == "native_exit":
                evidence = json.loads(marker.read_text()) if marker.exists() else {}
                if evidence.get("snapshots", 0) < 1 or evidence.get("entries", 0) < 2:
                    result["returncode"] = 1
                    result["evidence_error"] = "exit_flush_missing"
            elif scenario in ("fast_exit", "exit_registration_failure", "exit_snapshot_failure"):
                if marker.exists():
                    result["returncode"] = 1
                    result["evidence_error"] = "unexpected_exit_flush"
                if scenario.startswith("exit_"):
                    expected = "exit_registration_failed" if scenario == "exit_registration_failure" else "exit_snapshot_failed"
                    if not event_log.exists() or expected not in event_log.read_text():
                        result["returncode"] = 1
                        result["evidence_error"] = "missing_failure_diagnostic"
        result["elapsed_ns"] = time.monotonic_ns() - start
        results.append(result)
        print(f"{scenario}: {'PASS' if result['returncode'] == 0 else 'FAIL'}")
    (out / "report.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(result["returncode"] != 0 for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
