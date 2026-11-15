# SPDX-License-Identifier: MIT
"""rmm_86box: a Python client for 86Box's remote machine monitor (RMM).

Stdlib only. Connect to a VM started with `[General] rmm_port = N` in its 86box.cfg (or
`86Box --monitor-port N`), then drive it:

    from rmm_86box import Rmm
    with Rmm(55860) as rmm:
        print(rmm.machine.status())
        rmm.input.text.type(text="dir\\n")
        frame = rmm.screenshot()                  # RGB24, the guest keeps running
        rmm.snapshot.save(tag="a"); rmm.snapshot.restore(tag="a")
        rmm.vm.save("mybox")                      # the VM config as a KVM-style domain

Modules: rpc (thread-safe JSON-RPC client), client (Rmm facade), registry (methods, params,
results), complete (tab completion), console (line parsing and formatting), frames
(background frame polling), kvm (86box.cfg <-> libvirt-style domain).
"""

from .client import Frame, Rmm, VmConfig, abs_event, btn_event, key_event, rel_event
from .complete import Completer, Completion
from .console import Console, format_result, parse_call
from .frames import FramePoller
from .kvm import DomainStore, from_cfg, to_cfg
from .registry import Methods
from .rpc import RpcClient, RpcError

__version__ = "0.1.0"

__all__ = ["Rmm", "Frame", "VmConfig", "key_event", "rel_event", "abs_event", "btn_event", "RpcClient", "RpcError", "Methods", "Completer", "Completion",
           "Console", "parse_call", "format_result", "FramePoller", "DomainStore", "from_cfg", "to_cfg"]
