"""Constants and session state (caches shared across modules)"""


# Attribute name storing the persistent vertex ID (0 = unassigned)
ID_ATTR = "el_id"
# Marker custom property for comparison duplicates
COMPARE_PROP = "el_compare_of"
# Minimum distance considered a move
EPS = 1e-6

# Pre-edit snapshot while recording (object name -> {"pre": snapshot, "uid": int})
_recording = {}
# Warnings from the last rebuild (object name -> list[str])
_last_warnings = {}
# State of the last rebuild (object name -> {"fp": fingerprint, "uids": applied layers, "branch": int})
# Used to detect and adopt unrecorded edits
_last_state = {}
# Names of stack objects confirmed to have no shape keys during this session.
# If a key appears on an object in this set, it must have just been added, so
# the lock (automatic removal) can fire. Objects not in the set (files saved
# by older versions etc.) fall back to guard + warning mode to keep their data.
_no_key_confirmed = set()
# Notices about blocked shape key additions (object name -> True)
_blocked_notice = {}
# Names of duplicates created by the compare feature in this session. The
# marker (COMPARE_PROP) is copied on object duplication, so only names in this set may be deleted
_compare_names = set()
# Counter bumped on every rebuild (invalidates the influence highlight cache)
_rebuild_serial = [0]
# Influence highlight cache {"key": tuple, "data": (moved coords, created coords)}
_influence_cache = {}

# Colors assigned to branches automatically (cycled in creation order)
_BRANCH_PALETTE = (
    (0.85, 0.35, 0.35),  # red
    (0.35, 0.65, 0.95),  # blue
    (0.45, 0.80, 0.45),  # green
    (0.95, 0.75, 0.35),  # orange
    (0.75, 0.50, 0.90),  # purple
    (0.40, 0.85, 0.80),  # teal
    (0.95, 0.55, 0.75),  # pink
    (0.70, 0.70, 0.70),  # gray
)


def _assign_branch_color(stack, branch):
    branch.color = _BRANCH_PALETTE[(len(stack.branches) - 1) % len(_BRANCH_PALETTE)]
