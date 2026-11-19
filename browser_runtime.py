"""Use installed Chrome and its native Python runtime, including from WSL."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def chrome_executable():
    if sys.platform == 'win32':
        for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            base = os.environ.get(variable)
            if base:
                path = Path(base) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
                if path.is_file():
                    return str(path)
    elif sys.platform == 'darwin':
        path = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        if path.is_file():
            return str(path)
    else:
        for name in ('google-chrome', 'google-chrome-stable'):
            path = shutil.which(name)
            if path:
                return path
        if Path('/opt/google/chrome/chrome').is_file():
            return '/opt/google/chrome/chrome'
    raise FileNotFoundError('未找到本系统安装的正式版 Google Chrome。')


def relay_windows_chrome(argv):
    """A Linux Playwright driver cannot directly control Windows chrome.exe."""
    if sys.platform != 'linux' or 'microsoft' not in platform.release().lower():
        return None
    try:
        chrome_executable()
        return None
    except FileNotFoundError:
        pass
    installed = any(Path(path).is_file() for path in (
        '/mnt/c/Program Files/Google/Chrome/Application/chrome.exe',
        '/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    ))
    if not installed:
        return None
    python = ROOT / '.venv-windows' / 'Scripts' / 'python.exe'
    if not python.is_file():
        raise FileNotFoundError('检测到Windows Chrome，但缺少项目的.venv-windows 环境。请按初始化。')
    script = subprocess.check_output(['wslpath', '-w', str(ROOT / 'run_posts.py')], text=True).strip()
    print('使用Windows Python和已安装的正式版 Chrome。', flush=True)
    return subprocess.call([str(python), '-X', 'utf8', script, *argv])
