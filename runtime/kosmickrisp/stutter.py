#!/usr/bin/env python3
"""Measure KosmicKrisp + upstream DXVK stutter in a private Wine prefix.

  stutter.py bundle  --build-dir DIR --runtime DIR --out DIR
  stutter.py run     --runtime DIR --bundle DIR --bottle DIR --game NAME --work DIR
  stutter.py analyze RUN_DIR [RUN_DIR ...]

`run` never writes to the source bottle: the game directory is APFS-cloned
under --work and edits apply only to the clone. Games marked "steam" run
the clone inside the live bottle so steam_api reaches its running client;
only environment variables change there, DXVK DLLs go next to the cloned
executable, DXVK caches are redirected, and only the game process is
stopped, never the bottle's wineserver.
The driver writes MESA_KK_PRESENT_LOG and MESA_KK_COMPILE_LOG on one
monotonic clock, so frame-time hitches can be attributed to compiles.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time

GAMES_FILE = Path(__file__).with_name("stutter-games.json")
OVERRIDES = "d3d8,d3d9,d3d10core,d3d11,dxgi=n"
DRAW_KINDS = {"pso", "pso_wait", "variant_wait", "fs_variant",
              "fs_variant_wait", "rast_points"}
CREATE_KINDS = {"shaders", "deserialize"}
HITCH_FLOOR_MS = 50.0
HITCH_FACTOR = 3.0


def load_presents(path):
    starts = []
    with open(path, newline="") as stream:
        for row in csv.reader(stream):
            if len(row) >= 2 and row[0].lstrip("-").isdigit():
                starts.append(int(row[0]))
    return starts


def load_compiles(path):
    events = []
    if not path.is_file():
        return events
    with open(path, newline="") as stream:
        for row in csv.reader(stream):
            if len(row) == 4 and row[2].isdigit() and row[3].isdigit():
                events.append({"kind": row[0], "origin": row[1],
                               "start": int(row[2]), "end": int(row[3])})
    return events


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def overlap(a_start, a_end, b_start, b_end):
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def covered(a, b, intervals):
    clipped = sorted((max(a, s), min(b, e)) for s, e in intervals if s < b and e > a)
    total, end = 0, a
    for s, e in clipped:
        s = max(s, end)
        if e > s:
            total += e - s
            end = e
    return total


def analyze_frames(starts, events, skip_seconds=0.0):
    if len(starts) < 3:
        return {"frames": len(starts), "error": "fewer than three presents"}
    begin = starts[0] + int(skip_seconds * 1e9)
    frames = [(a, b) for a, b in zip(starts, starts[1:]) if a >= begin]
    if len(frames) < 2:
        return {"frames": len(frames), "error": "no frames after the skipped interval"}
    times = [(b - a) / 1e6 for a, b in frames]
    median = statistics.median(times)
    threshold = max(HITCH_FLOOR_MS, HITCH_FACTOR * median)
    slowest = sorted(times, reverse=True)[:max(1, len(times) // 100)]
    hitches = []
    for (a, b), ms in zip(frames, times):
        if ms < threshold:
            continue
        draw = covered(a, b, [(e["start"], e["end"]) for e in events
                              if e["kind"] in DRAW_KINDS and e["origin"] == "draw"]) / 1e6
        create = covered(a, b, [(e["start"], e["end"]) for e in events
                                if e["kind"] in CREATE_KINDS]) / 1e6
        combined = covered(a, b, [(e["start"], e["end"]) for e in events
                                   if e["kind"] in CREATE_KINDS or
                                   (e["kind"] in DRAW_KINDS and
                                    e["origin"] == "draw")]) / 1e6
        excess = ms - median
        hitches.append({"at_s": round((a - starts[0]) / 1e9, 3), "ms": round(ms, 2),
                        "draw_compile_ms": round(draw, 2),
                        "create_compile_ms": round(create, 2),
                        "compile_overlap_ms": round(combined, 2),
                        "explained": combined >= 0.5 * excess})
    duration = (frames[-1][1] - frames[0][0]) / 1e9
    return {
        "frames": len(times),
        "seconds": round(duration, 2),
        "mean_fps": round(len(times) / duration, 2) if duration else None,
        "median_ms": round(median, 2),
        "p95_ms": round(percentile(times, 0.95), 2),
        "p99_ms": round(percentile(times, 0.99), 2),
        "max_ms": round(max(times), 2),
        "one_percent_low_fps": round(1000.0 / statistics.mean(slowest), 2),
        "hitch_threshold_ms": round(threshold, 2),
        "hitches": len(hitches),
        "hitch_ms_total": round(sum(h["ms"] for h in hitches), 2),
        "hitches_explained_by_compiles": sum(h["explained"] for h in hitches),
        "worst_hitches": sorted(hitches, key=lambda h: -h["ms"])[:10],
    }


def summarize_compiles(events):
    table = {}
    for e in events:
        key = f'{e["kind"]}/{e["origin"]}'
        entry = table.setdefault(key, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        ms = (e["end"] - e["start"]) / 1e6
        entry["count"] += 1
        entry["total_ms"] += ms
        entry["max_ms"] = max(entry["max_ms"], ms)
    for entry in table.values():
        entry["total_ms"] = round(entry["total_ms"], 2)
        entry["max_ms"] = round(entry["max_ms"], 2)
    return dict(sorted(table.items()))


def parse_dxvk_logs(directory):
    result = {"logs": [], "gpl": None, "version": None, "rejections": [], "errors": 0}
    for log in sorted(directory.glob("*.log")):
        if not re.search(r"_(d3d9|d3d8|d3d11|dxgi)\.log$", log.name):
            continue
        result["logs"].append(log.name)
        for line in log.read_text(errors="replace").splitlines():
            version = re.search(r"DXVK: ((?:v|native-)\S+)", line)
            if version and not result["version"]:
                result["version"] = version.group(1)
            if "pipeline libraries not supported" in line.lower():
                result["gpl"] = False
            elif "pipeline libraries supported" in line.lower() and result["gpl"] is None:
                result["gpl"] = True
            if "does not support required" in line:
                result["rejections"].append(line.strip())
            if line.startswith("err:"):
                result["errors"] += 1
    return result


def analyze_run(run_dir, skip_seconds):
    events = load_compiles(run_dir / "compile.csv")
    present = run_dir / "present.csv"
    summary = {
        "run": run_dir.name,
        "frames": analyze_frames(load_presents(present), events, skip_seconds)
                  if present.is_file() else {"error": "no present log"},
        "compiles": summarize_compiles(events),
        "prewarm_events": sum(e["origin"] == "prewarm" for e in events),
        "dxvk": parse_dxvk_logs(run_dir / "dxvk"),
    }
    meta = run_dir / "run.json"
    if meta.is_file():
        summary["meta"] = json.loads(meta.read_text())
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def print_summary(summary):
    f = summary["frames"]
    d = summary["dxvk"]
    print(f'== {summary["run"]}')
    print(f'  DXVK {d["version"]}  GPL={d["gpl"]}  logs={",".join(d["logs"]) or "none"}')
    for line in d["rejections"]:
        print(f"  REJECTED: {line}")
    if "error" in f:
        print(f'  frames: {f["error"]}')
    else:
        print(f'  {f["frames"]} frames / {f["seconds"]} s  mean {f["mean_fps"]} fps  '
              f'1% low {f["one_percent_low_fps"]} fps')
        print(f'  frame ms: median {f["median_ms"]}  p95 {f["p95_ms"]}  p99 {f["p99_ms"]}  '
              f'max {f["max_ms"]}')
        print(f'  hitches >= {f["hitch_threshold_ms"]} ms: {f["hitches"]} '
              f'({f["hitch_ms_total"]} ms), {f["hitches_explained_by_compiles"]} '
              f'overlap compiles')
        for h in f["worst_hitches"][:5]:
            print(f'    t={h["at_s"]}s {h["ms"]} ms  draw-compile {h["draw_compile_ms"]} ms  '
                  f'create-compile {h["create_compile_ms"]} ms')
    print(f'  prewarm compiles: {summary["prewarm_events"]}')
    for key, entry in summary["compiles"].items():
        print(f'  {key:24} n={entry["count"]:<6} total {entry["total_ms"]:>10} ms  '
              f'max {entry["max_ms"]} ms')


def cmd_bundle(args):
    build = Path(args.build_dir)
    runtime_lib = Path(args.runtime) / "Wine/lib/kosmickrisp"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    driver = build / "src/kosmickrisp/vulkan/libvulkan_kosmickrisp.dylib"
    arch = subprocess.run(["lipo", "-archs", str(driver)], capture_output=True,
                          text=True, check=True).stdout.split()
    if arch != ["x86_64"]:
        sys.exit(f"{driver} is {arch}, but the runtime's Wine is x86_64")
    shutil.copy2(driver, out / driver.name)
    zlib = sorted(build.glob("subprojects/zlib-*/libz.1.dylib"))
    links = subprocess.run(["otool", "-L", str(out / driver.name)], capture_output=True,
                           text=True, check=True).stdout
    if "@rpath/libz.1.dylib" in links:
        if not zlib:
            sys.exit("driver links @rpath/libz.1.dylib but the build has no zlib")
        shutil.copy2(zlib[0], out / "libz.1.dylib")
        subprocess.run(["install_name_tool", "-change", "@rpath/libz.1.dylib",
                        "@loader_path/libz.1.dylib", str(out / driver.name)], check=True)
        subprocess.run(["codesign", "-f", "-s", "-", str(out / driver.name)], check=True,
                       capture_output=True)
    shutil.copy2(runtime_lib / "libvulkan.1.dylib", out / "libvulkan.1.dylib")
    manifest = json.loads((runtime_lib / "kosmickrisp_icd.json").read_text())
    manifest["ICD"]["library_path"] = "./libvulkan_kosmickrisp.dylib"
    (out / "kosmickrisp_icd.json").write_text(json.dumps(manifest, indent=2) + "\n")
    revision = subprocess.run(["git", "-C", str(Path(args.source)), "describe", "--always",
                               "--dirty"], capture_output=True, text=True).stdout.strip()
    (out / "SOURCE.txt").write_text(f"build={build}\nrevision={revision}\n")
    print(f"bundle ready: {out}")


def wine_env(runtime, bundle, prefix):
    env = {k: v for k, v in os.environ.items()
           if k not in ("WINE_VULKAN_LIBRARY", "VK_ICD_FILENAMES", "VK_ADD_DRIVER_FILES",
                        "VK_LOADER_DRIVERS_SELECT", "VK_LOADER_DRIVERS_DISABLE")}
    env.update({
        "WINEPREFIX": str(prefix),
        "WINEDEBUG": "-all",
        "CX_LIBVULKAN": str(bundle / "libvulkan.1.dylib"),
        "CX_ACTIVE_GRAPHICS_BACKEND": "wined3d",
        "VK_DRIVER_FILES": str(bundle / "kosmickrisp_icd.json"),
        "WINEDLLOVERRIDES": OVERRIDES,
    })
    return env


def wine(runtime, name):
    return str(Path(runtime) / "Wine/bin" / name)


def prepare_prefix(args, env, prefix):
    if not (prefix / "drive_c/windows/syswow64").is_dir():
        print(f"initializing prefix {prefix}")
        prefix.mkdir(parents=True, exist_ok=True)
        subprocess.run([wine(args.runtime, "wine64"), "wineboot", "-i"], env=env,
                       check=True, timeout=600, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        subprocess.run([wine(args.runtime, "wineserver"), "-w"], env=env, timeout=300)
    if not (prefix / ".winecx-kosmickrisp-dxvk.json").is_file():
        helper = Path(args.runtime) / "Wine/share/kosmickrisp/dxvk-prefix.py"
        subprocess.run([sys.executable, str(helper), "install", str(prefix),
                        str(Path(args.runtime) / "DXVK3")], check=True)


DXVK_NAMES = ("d3d8.dll", "d3d9.dll", "d3d10core.dll", "d3d11.dll", "dxgi.dll")


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_game(game, bottle, target_root):
    source = Path(bottle) / "drive_c" / game["bottle_path"]
    target = target_root / game["name"]
    if not (source / game["executable"]).is_file():
        sys.exit(f"{source / game['executable']} not found in the bottle")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"cloning {source} -> {target}")
        subprocess.run(["cp", "-c", "-R", str(source), str(target)], check=True)
    for name, text in game.get("write_files", {}).items():
        (target / name).write_text(text)
    for edit in game.get("edits", []):
        path = target / edit["file"]
        if path.is_file():
            data = path.read_text(errors="replace")
            new = data.replace(edit["old"], edit["new"])
            if new != data:
                path.unlink()
                path.write_text(new)
    return target


def place_dxvk(game, runtime, game_dir):
    payload = Path(runtime) / "DXVK3" / game.get("dxvk_arch", "x32")
    dll_dir = game_dir / game.get("dxvk_dir", str(Path(game["executable"]).parent))
    wanted = set(game.get("dxvk_dlls", []))
    for name in DXVK_NAMES:
        for stale_dir in {game_dir, (game_dir / game["executable"]).parent} - {dll_dir}:
            stale = stale_dir / name
            if stale.is_file() and (payload / name).is_file() and \
                    file_digest(stale) == file_digest(payload / name):
                stale.unlink()
        placed = dll_dir / name
        source = payload / name
        if name in wanted:
            shutil.copy2(source, placed)
        elif placed.is_file() and source.is_file() and file_digest(placed) == file_digest(source):
            placed.unlink()


def wineserver_running(prefix):
    info = Path(prefix).stat()
    server = Path(f"/tmp/.wine-{os.getuid()}") / f"server-{info.st_dev:x}-{info.st_ino:x}"
    return (server / "socket").exists()


def stop_prefix(runtime, env, process):
    subprocess.run([wine(runtime, "wineserver"), "-k"], env=env, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def stop_game(process):
    for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 15)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


STEAM_EXE = "C:\\Program Files (x86)\\Steam\\steam.exe"
BACKUP_SUFFIX = ".kk-stutter-backup"


def find_processes(pattern):
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(pid) for pid in result.stdout.split()]


def wait_processes(pattern, present, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(find_processes(pattern)) == present:
            return True
        time.sleep(1)
    return False


def signal_processes(pattern):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in find_processes(pattern):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if wait_processes(pattern, False, 15):
            return


def steam_shutdown(runtime, env):
    if not find_processes(r"Steam[/\\]steam\.exe"):
        return
    subprocess.run([wine(runtime, "wine64"), STEAM_EXE, "-shutdown"], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if not wait_processes(r"Steam[/\\]steam\.exe", False, 90):
        signal_processes(r"Steam[/\\]steam\.exe")
    wait_processes(r"steamwebhelper\.exe", False, 30)


def steam_start(runtime, env, extra, log):
    return subprocess.Popen([wine(runtime, "wine64"), STEAM_EXE, "-silent", *extra],
                            env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)


def place_dxvk_in_install(game, runtime, game_dir):
    payload = Path(runtime) / "DXVK3" / game.get("dxvk_arch", "x32")
    dll_dir = game_dir / game.get("dxvk_dir", str(Path(game["executable"]).parent))
    placed = []
    for name in game.get("dxvk_dlls", []):
        target = dll_dir / name
        backup = target.with_name(name + BACKUP_SUFFIX)
        if target.is_file() and not backup.exists():
            target.rename(backup)
        shutil.copy2(payload / name, target)
        placed.append(target)
    return placed


def restore_install(placed):
    for target in placed:
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        if target.is_file():
            target.unlink()
        if backup.is_file():
            backup.rename(target)


def run_via_steam(args, game, base_env, run_env, run_dir, seconds):
    exe_pattern = re.escape(Path(game["executable"]).name)
    steam_shutdown(args.runtime, base_env)
    with open(run_dir / "wine.log", "w") as log:
        steam_start(args.runtime, run_env,
                    ["-applaunch", str(game["steam_appid"]), *game.get("arguments", [])], log)
        if not wait_processes(exe_pattern, True, 180):
            print("  game did not start within 180 s")
            steam_shutdown(args.runtime, base_env)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not find_processes(exe_pattern):
                print("  game exited early")
                break
            time.sleep(1)
        signal_processes(exe_pattern)
    steam_shutdown(args.runtime, base_env)


def cmd_run(args):
    games = json.loads(GAMES_FILE.read_text())
    if args.game not in games:
        sys.exit(f"unknown game {args.game}; known: {', '.join(games)}")
    game = games[args.game]
    work = Path(args.work).expanduser().resolve()
    bundle = Path(args.bundle).resolve()
    live = game.get("steam", False)
    via_steam = live and args.via_steam
    placed = []
    if via_steam:
        prefix = Path(args.bottle).expanduser().resolve()
        if not wineserver_running(prefix):
            sys.exit("this game needs the bottle's Steam running; start Steam in the bottle first")
        if "steam_appid" not in game:
            sys.exit(f"{args.game} has no steam_appid")
        env = wine_env(args.runtime, bundle, prefix)
        env.update({"WINEMSYNC": "1", "WINEESYNC": "1"})
        env["WINEDLLOVERRIDES"] = game.get("dll_overrides", "")
        game_dir = prefix / "drive_c" / game["bottle_path"]
        placed = place_dxvk_in_install(game, args.runtime, game_dir)
    elif live:
        prefix = Path(args.bottle).expanduser().resolve()
        if not wineserver_running(prefix):
            sys.exit("this game needs the bottle's Steam running; start Steam in the bottle first")
        env = wine_env(args.runtime, bundle, prefix)
        env.update({"WINEMSYNC": "1", "WINEESYNC": "1"})
        env["WINEDLLOVERRIDES"] = game.get("dll_overrides", "")
        game_dir = prepare_game(game, args.bottle, work / "games")
        place_dxvk(game, args.runtime, game_dir)
        executable = str(game_dir / game["executable"])
    else:
        prefix = work / "prefix"
        env = wine_env(args.runtime, bundle, prefix)
        prepare_prefix(args, env, prefix)
        game_dir = prepare_game(game, args.bottle, prefix / "drive_c/kk-stutter")
        executable = "C:\\kk-stutter\\" + game["name"] + "\\" + game["executable"].replace("/", "\\")
    cache = work / "mesa-cache"
    state_cache = work / "dxvk-state-cache"
    shader_cache = work / "dxvk-shader-cache"
    seconds = args.seconds or game["seconds"]
    try:
        return run_labels(args, game, env, game_dir, prefix, live, via_steam, bundle,
                          work, cache, state_cache, shader_cache, seconds,
                          None if via_steam else executable)
    finally:
        if via_steam:
            restore_install(placed)
            steam_shutdown(args.runtime, env)
            steam_start(args.runtime, env, [], subprocess.DEVNULL)


def run_labels(args, game, env, game_dir, prefix, live, via_steam, bundle,
               work, cache, state_cache, shader_cache, seconds, executable):
    summaries = []
    for label in args.runs.split(","):
        if label.split("-")[0] == "cold":
            stale_dirs = [cache, state_cache, shader_cache]
            if not live:
                stale_dirs += list((prefix / "drive_c/users").glob("*/AppData/Local/dxvk"))
            for directory in stale_dirs:
                if directory.exists():
                    shutil.rmtree(directory)
            if not via_steam:
                for stale in game_dir.rglob("*.dxvk-cache"):
                    stale.unlink()
        state_cache.mkdir(parents=True, exist_ok=True)
        shader_cache.mkdir(parents=True, exist_ok=True)
        run_dir = work / "runs" / f'{time.strftime("%Y%m%d-%H%M%S")}-{args.game}-{label}'
        (run_dir / "dxvk").mkdir(parents=True)
        run_env = dict(env)
        run_env.update({
            "MESA_KK_PRESENT_LOG": str(run_dir / "present.csv"),
            "MESA_KK_COMPILE_LOG": str(run_dir / "compile.csv"),
            "MESA_SHADER_CACHE_DIR": str(cache),
            "DXVK_STATE_CACHE_PATH": str(state_cache),
            "DXVK_SHADER_CACHE_PATH": str(shader_cache),
            "DXVK_LOG_PATH": str(run_dir / "dxvk"),
            "DXVK_LOG_LEVEL": "info",
        })
        if args.cache_log:
            run_env["MESA_KK_CACHE_LOG"] = str(run_dir / "cache.jsonl")
        run_env.update(game.get("env", {}))
        for item in args.env:
            key, _, value = item.partition("=")
            run_env[key] = value
        meta = {"game": args.game, "label": label, "seconds": seconds, "live_bottle": live,
                "bundle": (bundle / "SOURCE.txt").read_text() if (bundle / "SOURCE.txt").is_file() else None,
                "arguments": game.get("arguments", []),
                "env": {k: run_env[k] for k in sorted(run_env)
                        if k.startswith(("MESA_", "DXVK_", "VK_", "CX_", "WINEDLL", "WINEMSYNC", "WINEESYNC"))}}
        (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
        print(f"running {args.game} ({label}) for {seconds} s -> {run_dir}")
        if via_steam:
            run_via_steam(args, game, env, run_env, run_dir, seconds)
            summary = analyze_run(run_dir, game.get("skip_seconds", 0.0))
            print_summary(summary)
            summaries.append(summary)
            continue
        with open(run_dir / "wine.log", "w") as log:
            launched_at = time.time()
            process = subprocess.Popen([wine(args.runtime, "wine64"), executable, *game.get("arguments", [])],
                                       env=run_env, cwd=(game_dir / game["executable"]).parent,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=seconds)
                termination = "natural_exit"
                print(f"  game exited early with code {process.returncode}")
            except subprocess.TimeoutExpired:
                termination = "time_limit"
            finally:
                if live:
                    stop_game(process)
                else:
                    stop_prefix(args.runtime, run_env, process)
            meta["termination"] = {"reason": termination,
                                   "returncode": process.returncode,
                                   "pid": process.pid,
                                   "elapsed_seconds": time.time() - launched_at}
            (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
        summary = analyze_run(run_dir, game.get("skip_seconds", 0.0))
        print_summary(summary)
        summaries.append(summary)
    return summaries


def cmd_analyze(args):
    for run in args.runs:
        print_summary(analyze_run(Path(run), args.skip_seconds))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bundle")
    b.add_argument("--build-dir", required=True)
    b.add_argument("--runtime", required=True)
    b.add_argument("--source", default=".")
    b.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--runtime", required=True)
    r.add_argument("--bundle", required=True)
    r.add_argument("--bottle", required=True)
    r.add_argument("--game", required=True)
    r.add_argument("--work", default="~/Library/Caches/kk-stutter")
    r.add_argument("--seconds", type=int)
    r.add_argument("--runs", default="cold,warm")
    r.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("--cache-log", action="store_true")
    r.add_argument("--via-steam", action="store_true")
    a = sub.add_parser("analyze")
    a.add_argument("runs", nargs="+")
    a.add_argument("--skip-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)
    {"bundle": cmd_bundle, "run": cmd_run, "analyze": cmd_analyze}[args.command](args)


if __name__ == "__main__":
    main()
