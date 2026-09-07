"""Compatibility wrapper checks; provider behavior is tested in smolmachines."""

from harbor_smolvm import SmolvmCloudEnvironment, SmolvmEnvironment
from smol.harbor import SmolEnvironment


def test_provider_aliases_use_canonical_sdk_provider() -> None:
    assert issubclass(SmolvmEnvironment, SmolEnvironment)
    assert issubclass(SmolvmCloudEnvironment, SmolEnvironment)
    assert SmolvmEnvironment.type() == "smolvm"
    assert SmolvmCloudEnvironment.type() == "smolvm-cloud"
