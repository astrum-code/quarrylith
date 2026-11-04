# SPDX-License-Identifier: Apache-2.0
"""The laptop plugin: the placement for one host, the node the declared
document names. It registers as `host` in the `quarrylith.placement` group."""

from .seam import Placement


class HostPlacement(Placement):
    """The placement of one host. It fills the seam's four methods as the
    allocator is built; until then it is abstract and `placement_for("host")`
    only names it."""

    name = "host"
