#!/usr/bin/env python3
"""Universal, configuration-friendly build driver used by GitHub Actions."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable


EXCLUDED_DIRS = {
    ".git",
    ".github",
    ".gradle",
    ".idea",
    ".packager",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "venv",
}

RUNNERS = {
    "windows-x64": "windows-latest",
    "linux-x64": "ubuntu-latest",
    "macos-arm64": "macos-latest",
    "macos-x64": "macos-15-intel",
    "linux-arm64": "ubuntu-24.04-arm",
}

DEFAULT_VERSIONS = {
    "python": "3.12",
    "node": "22",
    "java": "21",
    "go": "stable",
    "dotnet": "10.0.x",
}


def log(message: str) -> None:
    print(f"[packager] {message}", flush=True)


def run(
    command: str | list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    display = command if isinstance(command, str) else " ".join(command)
    log(f"run ({cwd}): {display}")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=merged,
        shell=isinstance(command, str),
        check=check,
        text=True,
    )


def is_excluded(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in EXCLUDED_DIRS for part in parts)


def find(root: Path, names: Iterable[str]) -> list[Path]:
    wanted = set(names)
    result: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.name in wanted and not is_excluded(path, root):
            result.append(path)
    return sorted(result, key=lambda item: (len(item.relative_to(root).parts), str(item)))


def find_glob(root: Path, pattern: str) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if is_excluded(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
            result.append(path)
    return sorted(result)


def load_config(root: Path) -> dict[str, Any]:
    for name in ("packaging.json", ".packaging.json"):
        path = root / name
        if path.exists():
            log(f"using configuration: {path.name}")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"{name} must contain a JSON object")
            return data
    return {}


def component(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get("components", {}).get(name, {})
    return value if isinstance(value, dict) else {}


def resolve(root: Path, value: str | None, fallback: Path | None = None) -> Path | None:
    if value:
        path = (root / value).resolve()
        if root != path and root not in path.parents:
            raise ValueError(f"path escapes repository: {value}")
        return path
    return fallback


def package_json_info(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def detect_languages(root: Path, config: dict[str, Any]) -> list[str]:
    configured = config.get("languages")
    if isinstance(configured, list) and configured:
        return sorted({str(item).lower() for item in configured})

    languages: set[str] = set()
    python_markers = find(root, ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"])
    if python_markers or find(root, ["main.py", "app.py", "cli.py"]):
        languages.add("python")

    package_files = find(root, ["package.json"])
    for package_file in package_files:
        data = package_json_info(package_file)
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        languages.add("node")
        if "electron" in deps or "electron-builder" in deps:
            languages.add("electron")
        if "@tauri-apps/cli" in deps or "@tauri-apps/api" in deps:
            languages.update({"tauri", "rust"})
        if "@capacitor/core" in deps or (package_file.parent / "capacitor.config.json").exists():
            languages.add("android")
        if "react-native" in deps or (package_file.parent / "android" / "gradlew").exists():
            languages.add("android")
        scripts = data.get("scripts", {})
        if "electron" not in languages and "tauri" not in languages and "build" in scripts:
            languages.add("web")

    if find(root, ["go.mod"]):
        languages.add("go")
    if find(root, ["Cargo.toml"]):
        languages.add("rust")
    if find_glob(root, "*.csproj") or find_glob(root, "*.fsproj") or find_glob(root, "*.vbproj"):
        languages.add("dotnet")
    if find(root, ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"]):
        languages.add("java")
    if find(root, ["CMakeLists.txt"]):
        languages.add("cpp")
    if find(root, ["Package.swift"]):
        languages.add("swift")

    pubspecs = find(root, ["pubspec.yaml"])
    for pubspec in pubspecs:
        text = pubspec.read_text(encoding="utf-8", errors="ignore")
        if "sdk: flutter" in text or "flutter:" in text:
            languages.update({"flutter", "android"})

    gradle_files = find(root, ["build.gradle", "build.gradle.kts"])
    if find(root, ["gradlew", "gradlew.bat"]):
        gradle_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore") for path in gradle_files[:20]
        )
        if "com.android.application" in gradle_text or find_glob(root, "AndroidManifest.xml"):
            languages.add("android")

    return sorted(languages)


def project_name(root: Path, config: dict[str, Any]) -> str:
    name = str(config.get("name") or "").strip()
    if not name:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        name = repo.rsplit("/", 1)[-1] if repo else root.name
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in name)
    return safe.strip("-.") or "application"


def build_plan(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    languages = detect_languages(root, config)
    configured_targets = config.get("targets")
    if isinstance(configured_targets, list):
        targets = [str(item) for item in configured_targets]
    else:
        targets: list[str] = []
        desktop = {"python", "electron", "go", "rust", "dotnet", "java", "cpp", "swift", "flutter", "tauri"}
        if desktop.intersection(languages):
            targets = ["windows-x64", "linux-x64", "macos-arm64"]
        elif "node" in languages or "web" in languages:
            targets = ["linux-x64"]
        if languages == ["swift"]:
            targets = ["macos-arm64"]
        if "android" in languages:
            targets.append("android")

    desktop_rows = [
        {"target": target, "runner": RUNNERS[target]}
        for target in targets
        if target in RUNNERS
    ]
    if not desktop_rows:
        desktop_rows = [{"target": "none", "runner": "ubuntu-latest"}]

    versions = DEFAULT_VERSIONS | {
        str(key): str(value) for key, value in config.get("versions", {}).items()
    }
    return {
        "name": project_name(root, config),
        "languages": languages,
        "targets": targets,
        "desktop_matrix": {"include": desktop_rows},
        "has_desktop": any(target in RUNNERS for target in targets),
        "has_android": "android" in targets,
        "versions": versions,
    }


def write_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def command_items(config: dict[str, Any], target: str) -> list[Any]:
    commands = config.get("commands", {})
    if not isinstance(commands, dict):
        return []
    result: list[Any] = []
    for key in ("common", target):
        value = commands.get(key, [])
        if isinstance(value, (str, dict)):
            result.append(value)
        elif isinstance(value, list):
            result.extend(value)
    return result


def run_custom_commands(root: Path, config: dict[str, Any], target: str, out: Path) -> None:
    env = {
        "PACKAGING_TARGET": target,
        "PACKAGING_OUTPUT": str(out),
        "PACKAGING_ROOT": str(root),
    }
    for item in command_items(config, target):
        if isinstance(item, str):
            command, cwd = item, root
        elif isinstance(item, dict):
            command = str(item["run"])
            cwd = resolve(root, str(item.get("cwd", ".")), root) or root
            env.update({str(k): str(v) for k, v in item.get("env", {}).items()})
        else:
            raise ValueError(f"invalid command entry: {item!r}")
        run(command, cwd=cwd, env=env)


def copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)


def collect_patterns(root: Path, patterns: list[str], stage: Path) -> int:
    count = 0
    for pattern in patterns:
        for source in root.glob(pattern):
            if is_excluded(source, root) and ".packager" not in source.parts:
                continue
            relative = source.relative_to(root)
            copy_path(source, stage / "custom" / relative)
            count += 1
    return count


def artifact_patterns(config: dict[str, Any], target: str) -> list[str]:
    configured = config.get("artifacts", [])
    if isinstance(configured, list):
        return [str(item) for item in configured]
    if isinstance(configured, dict):
        values: list[str] = []
        for key in ("common", target):
            item = configured.get(key, [])
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, list):
                values.extend(str(value) for value in item)
        return values
    return []


def package_manager(directory: Path) -> tuple[str, list[str]]:
    if (directory / "pnpm-lock.yaml").exists():
        run(["npm", "install", "--global", "pnpm@10"], cwd=directory)
        return "pnpm", ["pnpm", "install", "--frozen-lockfile"]
    if (directory / "yarn.lock").exists():
        run(["corepack", "enable"], cwd=directory, check=False)
        return "yarn", ["yarn", "install", "--immutable"]
    if (directory / "package-lock.json").exists():
        return "npm", ["npm", "ci"]
    return "npm", ["npm", "install"]


def build_node(root: Path, config: dict[str, Any], target: str, stage: Path, languages: list[str]) -> None:
    package_files = find(root, ["package.json"])
    if not package_files:
        return
    configured_root = component(config, "node").get("root")
    package_file = resolve(root, configured_root, package_files[0].parent)
    if package_file and package_file.is_dir():
        package_file = package_file / "package.json"
    if not package_file or not package_file.exists():
        raise FileNotFoundError("Node package.json not found")
    directory = package_file.parent
    data = package_json_info(package_file)
    manager, install = package_manager(directory)
    run(install, cwd=directory)
    scripts = data.get("scripts", {})

    if "electron" in languages:
        script = "dist" if "dist" in scripts else "package" if "package" in scripts else None
        if script:
            run([manager, "run", script], cwd=directory)
        else:
            run(["npx", "--yes", "electron-builder@latest", "--publish", "never"], cwd=directory)
        for folder in (directory / "dist", directory / "release", directory / "out"):
            if folder.exists():
                copy_path(folder, stage / "electron" / folder.name)
        return

    if "tauri" in languages:
        if "tauri" in scripts:
            run([manager, "run", "tauri", "build"], cwd=directory)
        else:
            run(["npx", "--yes", "@tauri-apps/cli@latest", "build"], cwd=directory)
        bundle = directory / "src-tauri" / "target" / "release" / "bundle"
        if bundle.exists():
            copy_path(bundle, stage / "tauri")
        return

    if "build" in scripts:
        run([manager, "run", "build"], cwd=directory)

    node_config = component(config, "node")
    entry = node_config.get("entry") or data.get("bin") or data.get("main")
    if isinstance(entry, dict):
        entry = next(iter(entry.values()), None)
    candidates = [entry, "dist/index.js", "build/index.js", "main.js", "index.js", "src/index.js"]
    entry_path = next(
        (directory / str(item) for item in candidates if item and (directory / str(item)).exists()),
        None,
    )
    if entry_path and "web" not in languages:
        target_map = {
            "windows-x64": "node22-win-x64",
            "linux-x64": "node22-linux-x64",
            "linux-arm64": "node22-linux-arm64",
            "macos-x64": "node22-macos-x64",
            "macos-arm64": "node22-macos-arm64",
        }
        suffix = ".exe" if target.startswith("windows") else ""
        output = stage / "node" / f"{project_name(root, config)}{suffix}"
        output.parent.mkdir(parents=True, exist_ok=True)
        run(
            ["npx", "--yes", "@yao-pkg/pkg@latest", str(entry_path), "--targets", target_map[target], "--output", str(output)],
            cwd=directory,
        )
    else:
        copied = False
        for folder_name in ("dist", "build", "out"):
            folder = directory / folder_name
            if folder.exists():
                copy_path(folder, stage / "web" / folder_name)
                copied = True
        if not copied:
            log("Node project has no executable entry or web output; use packaging.json for custom commands")


def python_entry(root: Path, config: dict[str, Any]) -> Path | None:
    configured = component(config, "python").get("entry")
    if configured:
        return resolve(root, str(configured))
    candidates = find(root, ["main.py", "app.py", "cli.py", "__main__.py"])
    return candidates[0] if candidates else None


def build_python(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    entry = python_entry(root, config)
    if not entry:
        raise FileNotFoundError("Python entry not found; set components.python.entry in packaging.json")
    directory = entry.parent
    requirements = find(root, ["requirements.txt"])
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "pyinstaller", "build"], cwd=root)
    if requirements:
        run([sys.executable, "-m", "pip", "install", "-r", str(requirements[0])], cwd=requirements[0].parent)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        run([sys.executable, "-m", "pip", "install", "."], cwd=root, check=False)

    temp = root / ".packager" / f"pyinstaller-{target}"
    dist = temp / "dist"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        project_name(root, config),
        "--distpath",
        str(dist),
        "--workpath",
        str(temp / "work"),
        "--specpath",
        str(temp),
    ]
    python_config = component(config, "python")
    if python_config.get("windowed"):
        command.append("--windowed")
    add_data = python_config.get("addData", [])
    if isinstance(add_data, list):
        for item in add_data:
            if isinstance(item, dict) and item.get("source"):
                source = resolve(root, str(item["source"]))
                if source and source.exists():
                    command.extend(["--add-data", f"{source}{os.pathsep}{item.get('destination', source.name)}"])
    for folder_name in ("templates", "static"):
        folder = directory / folder_name
        if folder.exists():
            command.extend(["--add-data", f"{folder}{os.pathsep}{folder_name}"])
    command.append(str(entry))
    run(command, cwd=root)
    for output in dist.iterdir():
        copy_path(output, stage / "python" / output.name)


def build_go(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    modules = find(root, ["go.mod"])
    if not modules:
        return
    directory = resolve(root, component(config, "go").get("root"), modules[0].parent) or modules[0].parent
    suffix = ".exe" if target.startswith("windows") else ""
    output = stage / "go" / f"{project_name(root, config)}{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    run(["go", "build", "-trimpath", "-ldflags", "-s -w", "-o", str(output), "."], cwd=directory)


def build_rust(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    manifests = find(root, ["Cargo.toml"])
    if not manifests:
        return
    directory = resolve(root, component(config, "rust").get("root"), manifests[0].parent) or manifests[0].parent
    run(["cargo", "build", "--release"], cwd=directory)
    metadata = subprocess.check_output(["cargo", "metadata", "--format-version", "1", "--no-deps"], cwd=directory, text=True)
    data = json.loads(metadata)
    copied = 0
    for package in data.get("packages", []):
        for target_info in package.get("targets", []):
            if "bin" not in target_info.get("kind", []):
                continue
            suffix = ".exe" if target.startswith("windows") else ""
            binary = directory / "target" / "release" / f"{target_info['name']}{suffix}"
            if binary.exists():
                copy_path(binary, stage / "rust" / binary.name)
                copied += 1
    if not copied:
        raise FileNotFoundError("Cargo build succeeded but no binary target was found")


def build_dotnet(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    projects = find_glob(root, "*.csproj") + find_glob(root, "*.fsproj") + find_glob(root, "*.vbproj")
    if not projects:
        return
    configured = component(config, "dotnet").get("project")
    project = resolve(root, configured, projects[0]) or projects[0]
    rid = {
        "windows-x64": "win-x64",
        "linux-x64": "linux-x64",
        "linux-arm64": "linux-arm64",
        "macos-x64": "osx-x64",
        "macos-arm64": "osx-arm64",
    }[target]
    output = stage / "dotnet"
    output.mkdir(parents=True, exist_ok=True)
    run(
        [
            "dotnet",
            "publish",
            str(project),
            "-c",
            "Release",
            "-r",
            rid,
            "--self-contained",
            "true",
            "-p:PublishSingleFile=true",
            "-p:DebugType=None",
            "-o",
            str(output),
        ],
        cwd=project.parent,
    )


def build_java(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    gradle = find(root, ["gradlew", "gradlew.bat"])
    maven = find(root, ["pom.xml"])
    if gradle:
        wrapper = next((path for path in gradle if path.name == ("gradlew.bat" if os.name == "nt" else "gradlew")), gradle[0])
        if os.name != "nt":
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        command = [str(wrapper), "build", "--no-daemon"]
        run(command, cwd=wrapper.parent)
        jars = [path for path in wrapper.parent.rglob("*.jar") if "build" in path.parts]
    elif maven:
        run(["mvn", "--batch-mode", "package", "-DskipTests"], cwd=maven[0].parent)
        jars = list((maven[0].parent / "target").glob("*.jar"))
    else:
        return
    jars = [path for path in jars if not any(part in path.name for part in ("sources", "javadoc", "plain"))]
    if not jars:
        raise FileNotFoundError("Java build succeeded but no runnable JAR was found")
    jar = max(jars, key=lambda path: path.stat().st_size)
    app_dir = stage / "java"
    app_dir.mkdir(parents=True, exist_ok=True)
    result = run(
        ["jpackage", "--type", "app-image", "--input", str(jar.parent), "--main-jar", jar.name, "--dest", str(app_dir), "--name", project_name(root, config)],
        cwd=jar.parent,
        check=False,
    )
    if result.returncode != 0:
        copy_path(jar, app_dir / jar.name)
        (app_dir / "run.cmd").write_text(f'@echo off\r\njava -jar "%~dp0{jar.name}"\r\n', encoding="utf-8")
        launcher = app_dir / "run.sh"
        launcher.write_text(f'#!/usr/bin/env sh\njava -jar "$(dirname "$0")/{jar.name}"\n', encoding="utf-8")
        launcher.chmod(0o755)


def build_cpp(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    cmakes = find(root, ["CMakeLists.txt"])
    if not cmakes:
        return
    source = resolve(root, component(config, "cpp").get("root"), cmakes[0].parent) or cmakes[0].parent
    build_dir = root / ".packager" / f"cmake-{target}"
    run(["cmake", "-S", str(source), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"], cwd=root)
    run(["cmake", "--build", str(build_dir), "--config", "Release", "--parallel"], cwd=root)
    output = stage / "cpp"
    for path in build_dir.rglob("*"):
        if not path.is_file():
            continue
        executable = path.suffix.lower() == ".exe" or (os.name != "nt" and os.access(path, os.X_OK) and "." not in path.name)
        if executable:
            copy_path(path, output / path.name)
    if not output.exists():
        raise FileNotFoundError("CMake build succeeded but no executable was found")


def build_swift(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    if not target.startswith("macos"):
        return
    packages = find(root, ["Package.swift"])
    if not packages:
        return
    directory = packages[0].parent
    run(["swift", "build", "-c", "release"], cwd=directory)
    bin_path = subprocess.check_output(["swift", "build", "-c", "release", "--show-bin-path"], cwd=directory, text=True).strip()
    output = stage / "swift"
    for path in Path(bin_path).iterdir():
        if path.is_file() and os.access(path, os.X_OK) and path.suffix not in {".o", ".d"}:
            copy_path(path, output / path.name)


def build_flutter(root: Path, config: dict[str, Any], target: str, stage: Path) -> None:
    pubspecs = find(root, ["pubspec.yaml"])
    if not pubspecs:
        return
    directory = resolve(root, component(config, "flutter").get("root"), pubspecs[0].parent) or pubspecs[0].parent
    run(["flutter", "pub", "get"], cwd=directory)
    platform_name = target.split("-", 1)[0]
    run(["flutter", "config", f"--enable-{platform_name}-desktop"], cwd=directory, check=False)
    run(["flutter", "build", platform_name, "--release"], cwd=directory)
    candidates = {
        "windows": directory / "build" / "windows" / "x64" / "runner" / "Release",
        "linux": directory / "build" / "linux" / "x64" / "release" / "bundle",
        "macos": directory / "build" / "macos" / "Build" / "Products" / "Release",
    }
    source = candidates[platform_name]
    if not source.exists():
        raise FileNotFoundError(f"Flutter output not found: {source}")
    copy_path(source, stage / "flutter")


def build_android(root: Path, config: dict[str, Any], stage: Path, languages: list[str]) -> None:
    if "flutter" in languages:
        pubspecs = find(root, ["pubspec.yaml"])
        directory = resolve(root, component(config, "flutter").get("root"), pubspecs[0].parent) or pubspecs[0].parent
        run(["flutter", "pub", "get"], cwd=directory)
        run(["flutter", "build", "apk", "--release"], cwd=directory)
        run(["flutter", "build", "appbundle", "--release"], cwd=directory)
    else:
        package_files = find(root, ["package.json"])
        if package_files and "node" in languages:
            directory = package_files[0].parent
            manager, install = package_manager(directory)
            run(install, cwd=directory)
            scripts = package_json_info(package_files[0]).get("scripts", {})
            if "build" in scripts:
                run([manager, "run", "build"], cwd=directory)
            capacitor = (directory / "capacitor.config.json").exists() or (directory / "capacitor.config.ts").exists()
            if capacitor:
                if not (directory / "android").exists():
                    run(["npx", "cap", "add", "android"], cwd=directory)
                run(["npx", "cap", "sync", "android"], cwd=directory)

        wrappers = find(root, ["gradlew"])
        if not wrappers:
            raise FileNotFoundError("Android Gradle wrapper not found")
        wrapper = next((path for path in wrappers if path.parent.name == "android"), wrappers[0])
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        result = run([str(wrapper), "assembleRelease", "bundleRelease", "--no-daemon"], cwd=wrapper.parent, check=False)
        if result.returncode != 0:
            log("release build was unavailable; creating directly installable debug packages")
            run([str(wrapper), "assembleDebug", "bundleDebug", "--no-daemon"], cwd=wrapper.parent)

    outputs = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".apk", ".aab"} and not is_excluded(path, root)]
    if not outputs:
        outputs = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".apk", ".aab"}]
    if not outputs:
        raise FileNotFoundError("Android build produced no APK or AAB")
    for path in outputs:
        copy_path(path, stage / "android" / path.name)


def archive_stage(root: Path, config: dict[str, Any], target: str, stage: Path) -> Path:
    if not stage.exists() or not any(stage.rglob("*")):
        raise FileNotFoundError("build completed without collectable artifacts; configure artifacts in packaging.json")
    artifacts = root / ".packager" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    base_name = f"{project_name(root, config)}-{target}"
    if target.startswith("windows") or target == "android":
        output = artifacts / f"{base_name}.zip"
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage))
    else:
        output = artifacts / f"{base_name}.tar.gz"
        with tarfile.open(output, "w:gz") as archive:
            archive.add(stage, arcname=base_name)
    log(f"created artifact: {output}")
    return output


def build(root: Path, target: str) -> None:
    config = load_config(root)
    plan = build_plan(root, config)
    languages = plan["languages"]
    state = root / ".packager"
    stage = state / "stage" / target
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    commands = command_items(config, target)
    if commands:
        run_custom_commands(root, config, target, stage)
        patterns = artifact_patterns(config, target)
        if patterns:
            collect_patterns(root, patterns, stage)
    elif target == "android":
        build_android(root, config, stage, languages)
    else:
        if "flutter" in languages:
            build_flutter(root, config, target, stage)
        else:
            if "node" in languages:
                build_node(root, config, target, stage, languages)
            if "python" in languages:
                build_python(root, config, target, stage)
            if "go" in languages:
                build_go(root, config, target, stage)
            if "rust" in languages and "tauri" not in languages:
                build_rust(root, config, target, stage)
            if "dotnet" in languages:
                build_dotnet(root, config, target, stage)
            if "java" in languages and "android" not in languages:
                build_java(root, config, target, stage)
            if "cpp" in languages:
                build_cpp(root, config, target, stage)
            if "swift" in languages:
                build_swift(root, config, target, stage)
        patterns = artifact_patterns(config, target)
        if patterns:
            collect_patterns(root, patterns, stage)

    archive_stage(root, config, target, stage)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="source repository directory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect_parser = subparsers.add_parser("detect")
    detect_parser.add_argument("--github-output")
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--target", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if args.command == "detect":
        config = load_config(root)
        plan = build_plan(root, config)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if args.github_output:
            versions = plan["versions"]
            outputs = {
                "project_name": plan["name"],
                "languages": ",".join(plan["languages"]),
                "targets": ",".join(plan["targets"]),
                "matrix": json.dumps(plan["desktop_matrix"], separators=(",", ":")),
                "has_desktop": str(plan["has_desktop"]).lower(),
                "has_android": str(plan["has_android"]).lower(),
                "python_version": versions["python"],
                "node_version": versions["node"],
                "java_version": versions["java"],
                "go_version": versions["go"],
                "dotnet_version": versions["dotnet"],
            }
            write_github_outputs(Path(args.github_output), outputs)
        return 0

    build(root, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
