# SPDX-License-Identifier: MIT
"""Placeholder: frames (and optionally input) over RFB, on the VM's VNC unix socket.

screendump costs a file write per frame and has no "changed?" signal; RFB pushes only dirty
rectangles. Plan, stdlib only:
    RfbClient(vnc_path).connect()      3.8 handshake, security None, ClientInit shared
    SetPixelFormat 32bpp, SetEncodings [Raw, CopyRect, DesktopSize]
    update(incremental=True) -> Frame  keeps a framebuffer; FramePoller polls it, `fresh`
                                       False when no rectangle came
QemuMachine.m_screenshot switches to it when the VM was started with vnc=True.
"""


class RfbClient:
    def __init__(self, path):
        self.path = path

    def connect(self):
        raise NotImplementedError("VNC frames are not built yet; screendump is used")
