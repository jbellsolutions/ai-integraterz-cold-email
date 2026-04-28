"""Shared squad infrastructure. Resolves model profiles from config/models.yaml
and provides a thin Squad class that wraps forge.Spawner with squad-specific
base instructions and per-role profile selection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from forge import (
    Consensus,
    HookBus,
    Spawner,
    SwarmResult,
    SwarmSpec,
    ToolRegistry,
    Topology,
    load_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
LOCAL_PROFILES_DIR = CONFIG_DIR / "profiles"

# Forge's make_provider() calls load_profile() WITHOUT search_paths, so it only
# sees forge's bundled profiles dir — our local config/profiles/ is invisible.
# Monkey-patch load_profile so the local dir is searched first system-wide.
# This is import-side-effect on purpose: any code that imports a Squad gets it.
import forge.kernel.profile as _fkp
_orig_load_profile = _fkp.load_profile

def _patched_load_profile(name: str, search_paths=None):
    if search_paths is None:
        forge_default = Path(_fkp.__file__).parent.parent / "providers" / "profiles"
        search_paths = [LOCAL_PROFILES_DIR, forge_default]
    return _orig_load_profile(name, search_paths=search_paths)

_fkp.load_profile = _patched_load_profile
# also patch any modules that already imported the symbol by name
import forge.providers as _fp
if hasattr(_fp, "load_profile"):
    _fp.load_profile = _patched_load_profile


def load_routing() -> dict[str, str]:
    """Read config/models.yaml and return the active mode's role→profile map."""
    cfg = yaml.safe_load((CONFIG_DIR / "models.yaml").read_text())
    return cfg["modes"][cfg["active_mode"]]


def resolve_profile(role: str):
    """Look up a profile by role from active routing, searching local profiles
    first, then forge bundled profiles."""
    routing = load_routing()
    profile_name = routing[role]
    # forge's load_profile accepts search_paths — local profiles override forge's
    import forge.providers.profiles as fp

    forge_profiles_dir = Path(fp.__file__).parent if hasattr(fp, "__file__") else None
    search = [LOCAL_PROFILES_DIR]
    if forge_profiles_dir:
        search.append(forge_profiles_dir)
    return load_profile(profile_name, search_paths=search)


@dataclass
class Squad:
    """A squad is one or more sub-agents on a forge Spawner with a
    squad-specific base instruction. Subclass to define `name`, `instructions`,
    `roles` (list of role names from models.yaml), and `topology`.
    """

    name: str
    instructions: str
    roles: list[str]                                # e.g. ["research", "research", "research"]
    topology: Topology = Topology.PARALLEL_COUNCIL
    consensus: Consensus = Consensus.MAJORITY
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    hooks: HookBus | None = None
    max_turns: int = 8

    async def run(self, task: str) -> SwarmResult:
        spawner = Spawner(
            tools=self.tools,
            hooks=self.hooks,
            base_instructions=self.instructions,
            max_turns=self.max_turns,
        )
        # resolve_profile returns ProviderProfile but Spawner takes profile names;
        # we pass the names from routing (the role→profile mapping in config)
        routing = load_routing()
        members = [routing[r] for r in self.roles]
        spec = SwarmSpec(
            topology=self.topology,
            consensus=self.consensus,
            members=members,
            metadata={"squad": self.name},
        )
        return await spawner.run(task, spec)
