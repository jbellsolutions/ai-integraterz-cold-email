"""Phase 0 smoke test — verify forge boots and runs a parallel council against the mock provider.

If this fails, do NOT proceed building squads. Either fix forge or pivot to Claude Agent SDK.
"""
import asyncio

from forge import Spawner, SwarmSpec, Topology, Consensus
from forge import ToolRegistry


def test_forge_imports():
    """All required symbols are present."""
    from forge import Spawner, SwarmSpec, Topology, Consensus, AgentDef, load_profile
    assert Topology.PARALLEL_COUNCIL
    assert Consensus.MAJORITY


def test_parallel_council_smoke():
    """Run a 3-member PARALLEL_COUNCIL on the mock profile end-to-end."""
    spawner = Spawner(
        tools=ToolRegistry(),
        base_instructions="You are a smoke-test agent. Reply with 'OK' and the role.",
        max_turns=2,
    )
    spec = SwarmSpec(
        topology=Topology.PARALLEL_COUNCIL,
        consensus=Consensus.MAJORITY,
        members=["mock", "mock", "mock"],
    )
    result = asyncio.run(spawner.run("smoke test", spec))
    assert len(result.members) == 3
    assert result.verdict is not None


if __name__ == "__main__":
    test_forge_imports()
    print("imports: OK")
    test_parallel_council_smoke()
    print("parallel council: OK")
    print()
    print("Phase 0 smoke test PASSED — forge harness is alive. Building squads.")
