from __future__ import annotations
import json
import os
import re
import subprocess
from . import process
from pathlib import Path


def vdf_strings(text):
    return [s.replace('\\\\', '\\').replace('\\"', '"') for s in re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text) for s in s if s]


def parse_vdf(text):
    tokens = iter(vdf_strings(text))
    def block():
        result = {}
        for key in tokens:
            if key == "}":
                return result
            val = next(tokens)
            result[key] = block() if val == "{" else val
        return result
    return block()


def registry_values(key_path, hive="HKLM"):
    if os.name != "nt":
        return {}
    import winreg
    try:
        with winreg.OpenKey(getattr(winreg, "HKEY_LOCAL_MACHINE" if hive == "HKLM" else "HKEY_CURRENT_USER"), key_path) as key:
            return {winreg.EnumValue(key, i)[0]: winreg.EnumValue(key, i)[1] for i in range(winreg.QueryInfoKey(key)[1])}
    except OSError:
        return {}


def detect_blender(executable):
    result = process.run([str(executable), "--version"], capture_output=True, text=True,
                            errors="replace", timeout=20, creationflags=0x08000000 if os.name == "nt" else 0)
    match = re.search(r"Blender (\d+\.\d+\.\d+)", result.stdout)
    if not match:
        raise ValueError("无法读取 Blender 版本")
    return match.group(1)


def discover(manual=()):
    candidates = []
    def add(path, kind, source):
        if path and Path(path).is_file():
            candidates.append((Path(path).resolve(), kind, source))
    pf = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    for path in (pf / "Blender Foundation").glob("*/blender.exe"):
        add(path, "blender", "安装目录")
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for branch in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(hive, branch) as root:
                        for i in range(winreg.QueryInfoKey(root)[0]):
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                                try:
                                    name = winreg.QueryValueEx(key, "DisplayName")[0]
                                    if "Blender" in name:
                                        add(Path(winreg.QueryValueEx(key, "InstallLocation")[0]) / "blender.exe", "blender", "注册表")
                                except OSError:
                                    pass
                except OSError:
                    pass
    steam = registry_values(r"SOFTWARE\Valve\Steam", "HKCU").get("SteamPath")
    steam = steam or registry_values(r"SOFTWARE\WOW6432Node\Valve\Steam").get("InstallPath")
    roots = {Path(steam)} if steam else set()
    for guess in (Path("D:/Steam"), pf / "Steam", Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam"):
        if guess.exists():
            roots.add(guess)
    for root in list(roots):
        lib = root / "steamapps/libraryfolders.vdf"
        if lib.is_file():
            try:
                for value in parse_vdf(lib.read_text(encoding="utf-8"))["libraryfolders"].values():
                    if isinstance(value, dict) and value.get("path"):
                        roots.add(Path(value["path"]))
                    elif isinstance(value, str) and Path(value).is_dir():
                        roots.add(Path(value))
            except (KeyError, ValueError, StopIteration):
                pass
    for root in roots:
        manifest = root / "steamapps/appmanifest_365670.acf"
        if manifest.is_file():
            try:
                folder = parse_vdf(manifest.read_text(encoding="utf-8"))["AppState"]["installdir"]
                add(root / "steamapps/common" / folder / "blender.exe", "blender", "Steam")
            except (KeyError, ValueError, StopIteration):
                pass
    launcher = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Epic/UnrealEngineLauncher/LauncherInstalled.dat"
    engines = set()
    if launcher.is_file():
        for entry in json.loads(launcher.read_text(encoding="utf-8-sig")).get("InstallationList", []):
            engines.add(Path(entry["InstallLocation"]))
    engines.update((pf / "Epic Games").glob("UE_*"))
    engines.update(Path(p) for p in registry_values(r"SOFTWARE\Epic Games\Unreal Engine\Builds", "HKCU").values() if isinstance(p, str))
    for engine in engines:
        add(engine / "Engine/Binaries/Win64/UnrealEditor.exe", "unreal", "Epic / 注册表")
    for path in manual:
        add(path, "blender" if Path(path).name.lower() == "blender.exe" else "unreal", "手动")
    found, seen = [], set()
    for path, kind, source in sorted(candidates,key=lambda c:c[2] != "Steam"):
        identity = str(path).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if kind == "blender":
                version = detect_blender(path)
                ready = version == "4.3.2"
            else:
                version_file = path.parents[2] / "Build/Build.version"
                v = json.loads(version_file.read_text(encoding="utf-8-sig"))
                version = ".".join(str(v[x]) for x in ("MajorVersion", "MinorVersion", "PatchVersion"))
                ready = version.startswith("5.8.")
            found.append({"kind": kind, "path": str(path), "version": version,
                          "source": source, "compatibility": "验收目标" if ready else "实验性：需兼容检查"})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            continue
    return sorted(found, key=lambda x: (x["kind"], x["compatibility"] != "验收目标", x["version"]), reverse=False)
