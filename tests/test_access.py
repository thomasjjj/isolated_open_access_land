import pytest

from access_islands.access import barrier_blocks, pedestrian


@pytest.mark.parametrize(
    "tags,tier,seed",
    [
        ({"highway": "track"}, "unknown", False),
        ({"highway": "track", "access": "private"}, "excluded", False),
        ({"highway": "track", "access": "private", "foot": "yes"}, "public", False),
        ({"highway": "path", "designation": "public_footpath"}, "public", False),
        ({"highway": "path", "designation": "public_footpath", "foot": "no"}, "unknown", False),
        ({"highway": "motorway", "foot": "yes"}, "excluded", False),
        ({"highway": "residential"}, "inferred", True),
        ({"highway": "residential", "foot": "no"}, "excluded", False),
        ({"highway": "path", "foot": "permissive"}, "permissive", False),
        ({"highway": "path", "foot:conditional": "yes @ daylight"}, "unknown", False),
    ],
)
def test_pedestrian_policy(tags, tier, seed):
    assert pedestrian(tags)[:2] == (tier, seed)


def test_barriers():
    assert barrier_blocks({"barrier": "wall"})
    assert barrier_blocks({"barrier": "gate", "access": "private"})
    assert not barrier_blocks({"barrier": "stile"})
    assert not barrier_blocks({"barrier": "gate", "access": "private", "foot": "yes"})
