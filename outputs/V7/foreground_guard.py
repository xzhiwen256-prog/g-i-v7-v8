"""Fail-closed foreground executable check for the live preview."""
import json
import ntpath
from pathlib import Path

from input_diagnostic import foreground_identity

ROOT = Path(__file__).resolve().parent


class ForegroundGuard:
    def __init__(self, allowed_names, read_foreground=foreground_identity):
        self.allowed_names = frozenset(name.casefold() for name in allowed_names
                                       if isinstance(name, str) and name and ntpath.basename(name) == name)
        self.read_foreground = read_foreground

    def allowed(self):
        try:
            info = self.read_foreground()
            pid = info.get('pid')
            path = info.get('process')
            return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and
                    isinstance(path, str) and bool(path) and
                    ntpath.basename(path).casefold() in self.allowed_names)
        except Exception:
            return False


def load_foreground_guard(path=ROOT/'.settings/foreground-profile.json'):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        names = data['allowed_executable_names']
        if not isinstance(names, list):
            names = []
    except (OSError, ValueError, TypeError, KeyError):
        names = []
    return ForegroundGuard(names)
