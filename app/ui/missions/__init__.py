"""Mission widgets for the Py panel.

Two states of the same slot: :class:`MissionPicker` when nothing is active,
:class:`MissionCard` when something is. Neither owns Mission state - both read
from and call into MissionService, so the panel can be destroyed and rebuilt
without a Mission noticing.
"""

from app.ui.missions.mission_card import MissionCard
from app.ui.missions.mission_picker import MissionPicker

__all__ = ["MissionCard", "MissionPicker"]
