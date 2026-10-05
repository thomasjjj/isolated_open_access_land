"""Pedestrian evidence policy, shared by OSM import and synthetic fixtures."""

from __future__ import annotations

PUBLIC = {"public_footpath", "public_bridleway", "restricted_byway", "byway_open_to_all_traffic"}
ROADS = {
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "primary_link",
    "secondary_link",
    "tertiary_link",
    "pedestrian",
}
BLOCKED = {"no", "private", "customers", "delivery", "agricultural", "forestry", "use_sidepath"}
ALLOWED = {"yes", "designated", "official"}
PASSABLE_BARRIERS = {"gate", "kissing_gate", "stile", "entrance", "bollard", "cycle_barrier"}


def pedestrian(tags: dict, official=False) -> tuple[str, bool, str]:
    """Return tier, possible road seed, and review reason. No blanket highway assumption."""
    if official:
        return "official", False, ""
    highway = tags.get("highway", "")
    foot, access = tags.get("foot", ""), tags.get("access", "")
    designation = tags.get("designation", "")
    if highway in {"motorway", "motorway_link", "construction", "proposed"}:
        return "excluded", False, "pedestrian_prohibited"
    if designation in PUBLIC and foot in BLOCKED:
        return "unknown", False, "conflicting_access_tags"
    effective = foot or access
    if effective in BLOCKED:
        return "excluded", False, "pedestrian_prohibited"
    if tags.get("foot:conditional") or tags.get("access:conditional"):
        return "unknown", highway in ROADS, "conditional_access"
    if effective == "permissive":
        return "permissive", False, "permissive_only"
    if effective in ALLOWED or designation in PUBLIC:
        return "public", highway in ROADS, ""
    if highway in ROADS and not effective:
        return "inferred", True, "road_access_inferred"
    return "unknown", False, "access_not_recorded"


def barrier_blocks(tags: dict) -> bool:
    effective = tags.get("foot") or tags.get("access", "")
    if effective in BLOCKED:
        return True
    if tags.get("barrier") in PASSABLE_BARRIERS:
        return False
    return bool(tags.get("barrier")) and effective not in ALLOWED


def grade(tags: dict) -> str:
    return f"{tags.get('layer', '0')}:{tags.get('bridge', 'no')}:{tags.get('tunnel', 'no')}"
