"""Windows x64 便携包：只复制明确的产物，不读取用户资料或凭据。"""

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("Build on Windows x64")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    output = ROOT / "release"
    output.mkdir(exist_ok=True)
    bundle = output / f"Archive-Zero-{version}-windows-x64"
    if bundle.exists():
        raise SystemExit(f"Build directory exists; archive it before rebuilding: {bundle}")
    subprocess.run(["npm.cmd", "--prefix", "desktop", "run", "build"], cwd=ROOT, check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "archive-zero-backend",
            "--distpath",
            str(output / "backend"),
            "--workpath",
            str(output / "build"),
            "--specpath",
            str(output / "build"),
            "--paths",
            str(ROOT / "src"),
            "--collect-submodules",
            "reading_agent",
            "--collect-data",
            "reading_agent",
            "--collect-all",
            "uvicorn",
            "--add-data",
            str(ROOT / "src/reading_agent/fixtures.json") + ":reading_agent",
            str(ROOT / "scripts/frozen_backend.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    electron = ROOT / "desktop/node_modules/electron/dist"
    shutil.copytree(electron, bundle)
    (bundle / "electron.exe").rename(bundle / "Archive Zero.exe")
    app = bundle / "resources/app"
    shutil.copytree(ROOT / "desktop/electron", app / "electron")
    shutil.copytree(ROOT / "desktop/dist", app / "dist")
    metadata = json.loads((ROOT / "desktop/package.json").read_text())
    metadata = {key: metadata[key] for key in ("name", "productName", "version", "main")}
    (app / "package.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    shutil.copytree(output / "backend/archive-zero-backend", bundle / "resources/backend")
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE-Archive-Zero.txt")
    (bundle / "START-HERE.txt").write_text(
        "Archive Zero - Windows x64\nExtract ALL files, then open Archive Zero.exe.\n"
        "No Python or Node installation is required.\n"
        "Configure your own model/API key in Settings.\n"
        "Data: %APPDATA%/reading-agent-desktop/data (or AGENT_DATA_DIR).\n"
        "This preview is unsigned. Close hides to tray; use Quit to exit.\n",
        encoding="utf-8",
    )
    archive = output / (bundle.name + ".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(bundle.rglob("*")):
            if path.is_symlink():
                raise ValueError("Release must not contain symlinks")
            if path.is_file():
                zipped.write(path, path.relative_to(output))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(archive)


if __name__ == "__main__":
    main()
