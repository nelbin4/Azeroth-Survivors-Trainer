"""Build the reusable launcher and a release zip. Usage: python build_exe.py [--console]."""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'AzerothSurvivorsTrainer'
SHIPPED = ('game.py', 'README.md', 'CHANGELOG.md', 'LICENSE')


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
               '--collect-all', 'PyMemoryEditor', '--collect-all', 'frida',
               '--add-data', str(ROOT / 'profile.example.json') + ';.', str(ROOT / 'launcher' / 'launcher.py')]
    subprocess.run(command, check=True)
    release = ROOT / 'release' / f'{NAME}-{version()}'
    shutil.rmtree(release, ignore_errors=True)
    release.mkdir(parents=True)
    shutil.copy2(ROOT / 'dist' / f'{name}.exe', release / f'{name}.exe')
    console_exe = ROOT / 'dist' / f'{NAME}-console.exe'
    if not console and console_exe.is_file():
        shutil.copy2(console_exe, release / console_exe.name)
    for item in SHIPPED:
        shutil.copy2(ROOT / item, release / item)
    archive = ROOT / 'release' / f'{NAME}-{version()}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for path in release.rglob('*'):
            zipped.write(path, path.relative_to(release.parent))
    print(f'Built {release / (name + ".exe")} ({(release / (name + ".exe")).stat().st_size // 1048576} MB) and {archive}')


if __name__ == '__main__':
    main()
