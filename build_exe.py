"""Build the launcher exe and a release folder. Usage: python build_exe.py [--console]

The exe only bundles Python and the libraries; game.py is shipped next to it and loaded at run time.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'AzerothSurvivorsTrainer'
SHIPPED = ('game.py', 'profile.example.json', 'requirements.txt', 'README.md', 'CHANGELOG.md', 'LICENSE')


def version():
    for line in (ROOT / 'game.py').read_text(encoding='utf-8').splitlines():
        if line.startswith('VERSION = '):
            return line.split("'")[1]
    raise SystemExit('VERSION not found in game.py')


def main():
    console = '--console' in sys.argv
    name = NAME + ('-console' if console else '')
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
               '--console' if console else '--noconsole', '--name', name,
               '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'), '--specpath', str(ROOT / 'build'),
               '--collect-all', 'PyMemoryEditor', '--exclude-module', 'frida', str(ROOT / 'launcher' / 'launcher.py')]
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
