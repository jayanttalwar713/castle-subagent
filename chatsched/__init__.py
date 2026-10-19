"""Parallel request handling for a group-chat agent.

See DESIGN.md. The one-line thesis: serialize on dependency, not on arrival.
"""

__all__ = ["Sequencer", "Simulation", "get_policy"]

from .policies import get_policy
from .sequencer import Sequencer
from .sim import Simulation
