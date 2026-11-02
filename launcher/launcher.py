"""Launch the adjacent trainer source using the bundled Python runtime."""
import runpy
import sys
import os
import traceback
from datetime import datetime
from pathlib import Path

# game.py is loaded from disk, so PyInstaller cannot see what it imports. Everything it needs is listed here.
import argparse, atexit, ctypes, ctypes.wintypes, difflib, hashlib, io, json, math, re, shutil, struct  # noqa: E401,F401
import subprocess, tempfile, threading, time, unittest, urllib.request, uuid, zipfile  # noqa: E401,F401
import tkinter, tkinter.filedialog, tkinter.messagebox, tkinter.simpledialog, tkinter.ttk  # noqa: E401,F401
import numpy, psutil, PIL.Image, PIL.ImageOps, PyMemoryEditor, frida  # noqa: E401,F401


def folder():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent


def fail(message):
    try:
        import tkinter.messagebox as box
        box.showerror('Azeroth Survivors Trainer', message)
    except Exception:
        print(message, file=sys.stderr)
    sys.exit(1)


def main():
    game = folder() / 'game.py'
    if not game.is_file():
        fail(f'game.py was not found next to this program:\n{game.parent}')
    arguments = sys.argv[1:]
    if arguments and arguments[0].lower().endswith('game.py'):  # game.py re-launches itself as: <exe> game.py <command>
        arguments = arguments[1:]
    sys.argv = [str(game)] + arguments
    runpy.run_path(str(game), run_name='__main__')


if __name__ == '__main__':
    if not getattr(sys, 'frozen', False):
        main()
    else:
        # Windowed PyInstaller builds do not have stdout/stderr, including when
        # started from a terminal. Give every invocation a persistent log instead.
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with (folder() / 'trainer.log').open('a', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            try:
                command = sys.argv[1] if len(sys.argv) > 1 else 'gui'
                print(f'\n[{datetime.now().isoformat(timespec="seconds")}] PID {os.getpid()} command {command}', flush=True)
                main()
            except Exception:
                traceback.print_exc()
                raise
            finally:
                sys.stdout, sys.stderr = original_stdout, original_stderr
