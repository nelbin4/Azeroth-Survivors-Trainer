"""Azeroth Survivors Trainer launcher.

A deliberately tiny program: it bundles Python and the libraries, then runs game.py from the folder it
lives in. game.py stays a normal editable, auto-updating file, so updating the trainer never needs a
new exe (only a new dependency does).
"""
import runpy
import sys
from pathlib import Path

# game.py is loaded from disk, so PyInstaller cannot see what it imports. Everything it needs is listed here.
import argparse, atexit, ctypes, ctypes.wintypes, difflib, hashlib, io, json, math, re, shutil, struct  # noqa: E401,F401
import subprocess, tempfile, threading, time, unittest, urllib.request, uuid, zipfile  # noqa: E401,F401
import tkinter, tkinter.filedialog, tkinter.messagebox, tkinter.simpledialog, tkinter.ttk  # noqa: E401,F401
import numpy, psutil, PIL.Image, PIL.ImageOps, PyMemoryEditor  # noqa: E401,F401


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
        fail(f'game.py was not found next to this program:\n{game.parent}\n\nKeep AzerothSurvivorsTrainer.exe and game.py in the same folder.')
    arguments = sys.argv[1:]
    if arguments and arguments[0].lower().endswith('game.py'):  # game.py re-launches itself as: <exe> game.py <command>
        arguments = arguments[1:]
    sys.argv = [str(game)] + arguments
    runpy.run_path(str(game), run_name='__main__')


if __name__ == '__main__':
    main()
