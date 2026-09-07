"""Run Harbor agent-evaluation tasks on smolvm microVMs (local or smolfleet cloud).

Use via Harbor's ``--env`` import path:

    harbor run ... --env harbor_smolvm:SmolvmEnvironment       # local microVM
    harbor run ... --env harbor_smolvm:SmolvmCloudEnvironment  # smolfleet cloud
"""

from harbor_smolvm.environment import SmolvmCloudEnvironment, SmolvmEnvironment

__all__ = ["SmolvmEnvironment", "SmolvmCloudEnvironment"]
__version__ = "0.1.0"
