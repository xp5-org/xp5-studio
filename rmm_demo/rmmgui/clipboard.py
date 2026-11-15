# SPDX-License-Identifier: MIT
"""System clipboard for the demo, best effort: Qt's clipboard (set with use_qt() once the window
exists; Qt only touches it on the GUI thread, so the window supplies the marshalling), then a platform tool (wl-copy/xclip/xsel, pbcopy, clip.exe /
PowerShell), and always an in-process copy so cut/copy/paste inside the window works even
headless (Qt offscreen platform) or with no tool installed.
"""

import shutil
import subprocess
import sys

TOOLS_PUT = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
             ["pbcopy"], ["clip.exe"]]
TOOLS_GET = [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
             ["xsel", "--clipboard", "--output"], ["pbpaste"],
             ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard"]]


class Clipboard:
    def __init__(self, use_system=True):
        self.use_system = use_system
        self.local = ""
        self.backend = "local"
        self.qt = None

    def use_qt(self, put_text, get_text):
        """Route copy/paste through Qt: put_text(str), get_text() -> str, both safe to call from
        any thread."""
        self.qt = (put_text, get_text)

    def _scrap(self):
        return self.qt

    def put(self, text):
        self.local = text
        if not self.use_system or not text:
            return "local"
        sc = self._scrap()
        if sc:
            try:
                sc[0](text)
                self.backend = "qt"
                return self.backend
            except Exception:  # noqa: BLE001
                pass
        for cmd in TOOLS_PUT:
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, input=text.encode("utf-16le" if cmd[0] == "clip.exe" else "utf-8"),
                                   timeout=2, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.backend = cmd[0]
                    return self.backend
                except (OSError, subprocess.SubprocessError):
                    continue
        self.backend = "local"
        return self.backend

    def get(self):
        if self.use_system:
            sc = self._scrap()
            if sc:
                try:
                    t = sc[1]()
                    if t:
                        return t.replace("\x00", "")
                except Exception:  # noqa: BLE001
                    pass
            for cmd in TOOLS_GET:
                if shutil.which(cmd[0]):
                    try:
                        r = subprocess.run(cmd, capture_output=True, timeout=2)
                        if r.returncode == 0 and r.stdout:
                            t = r.stdout.decode("utf-8", "replace")
                            return t.rstrip("\r\n") if sys.platform.startswith("win") else t
                    except (OSError, subprocess.SubprocessError):
                        continue
        return self.local
