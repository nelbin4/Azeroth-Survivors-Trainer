"""Build the reusable windowed launcher and a release zip."""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'AzerothSurvivorsTrainer'
SHIPPED = ('game.py',)  # game.py carries the MIT notice at its top, so the zip needs no separate LICENSE file


def version():
    for line in (ROOT / 'game.py').read_text(encoding='utf-8').splitlines():
        if line.startswith('VERSION = '):
            return line.split("'")[1]
    raise SystemExit('VERSION not found in game.py')


def main():
    if len(sys.argv) > 1:raise SystemExit('Usage: python build_exe.py')
    name = NAME
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
               '--noconsole', '--name', name,
               '--icon', str(ROOT / 'assets' / 'trainer-icon.ico'),
               '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'), '--specpath', str(ROOT / 'build'),
               '--collect-all', 'PyMemoryEditor',
               '--add-data', str(ROOT / 'profile.example.json') + ';.',
               '--add-data', str(ROOT / 'assets' / 'trainer-icon.ico') + ';assets', str(ROOT / 'launcher' / 'launcher.py')]
    subprocess.run(command, check=True)
    release = ROOT / 'release' / f'{NAME}-{version()}'
    shutil.rmtree(release, ignore_errors=True)
    release.mkdir(parents=True)
    shutil.copy2(ROOT / 'dist' / f'{name}.exe', release / f'{name}.exe')
    for item in SHIPPED:
        shutil.copy2(ROOT / item, release / item)
    archive = ROOT / 'release' / f'{NAME}-{version()}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for path in release.rglob('*'):
            zipped.write(path, path.relative_to(release.parent))
    print(f'Built {release / (name + ".exe")} ({(release / (name + ".exe")).stat().st_size // 1048576} MB) and {archive}')


if __name__ == '__main__':
    main()
