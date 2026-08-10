"""Checks for duplicate merge / soft match / harden behavior."""

from __future__ import annotations

from scraper.store import (
    dedupe_archive,
    find_match_index,
    merge_archive,
    merge_record_fields,
    projects_soft_match,
)


def _unit(**kwargs):
    base = {
        "projectName": "Gaurs Atulayam",
        "city": "Greater Noida",
        "locality": "Sector 16",
        "bhk": 3,
        "superBuiltUpArea": 1500,
        "carpetArea": None,
        "bathrooms": None,
        "price": None,
        "amenities": [],
        "images": [],
        "highlights": [],
        "sources": [],
    }
    base.update(kwargs)
    return base


def test_null_fill_merge():
    existing = _unit(carpetArea=None, bathrooms=None, amenities=["Pool"])
    incoming = _unit(
        projectName="Gaurs Atulyam",
        carpetArea=1200,
        bathrooms=3,
        amenities=["Gym"],
        price=9_000_000,
    )
    merged, changed = merge_record_fields(existing, incoming)
    assert changed
    assert merged["carpetArea"] == 1200
    assert merged["bathrooms"] == 3
    assert merged["price"] == 9_000_000
    assert merged["amenities"] == ["Pool", "Gym"]
    # Never overwrite concrete with different concrete
    existing2 = _unit(price=8_000_000)
    merged2, _ = merge_record_fields(existing2, _unit(price=9_000_000))
    assert merged2["price"] == 8_000_000


def test_soft_project_match_and_archive_merge():
    assert projects_soft_match("Gaurs Atulayam", "Gaurs Atulyam")
    assert not projects_soft_match("Gaur City 1st Avenue", "Gaur City 4th Avenue")
    assert not projects_soft_match("Gaur Grandeur", "Gaur Grandeur 2")
    assert not projects_soft_match("Alpha One", "Alpha Two")  # short / different brand tail
    assert not projects_soft_match("Lodha Park", "Godrej Park")  # different brand token

    archive = [_unit(carpetArea=None, bathrooms=None)]
    batch = [_unit(projectName="Gaurs Atulyam", carpetArea=1100, bathrooms=2)]
    merged, added, updated = merge_archive(archive, batch)
    assert len(added) == 0
    assert len(updated) == 1
    assert len(merged) == 1
    assert merged[0]["carpetArea"] == 1100
    assert merged[0]["bathrooms"] == 2


def test_soft_match_requires_locality():
    archive = [_unit(locality="", carpetArea=None)]
    incoming = _unit(projectName="Gaurs Atulyam", locality="", carpetArea=1100)
    # Without locality, soft match must not fire — treat as separate unless exact key matches.
    # Exact keys differ because project slug differs (atulayam vs atulyam).
    assert find_match_index(archive, incoming) is None


def test_dedupe_archive_collapses_exact_dups():
    rows = [
        _unit(amenities=["A"]),
        _unit(amenities=["B"], carpetArea=1000),
        _unit(projectName="Other Project Name", bhk=2),
    ]
    out, merges, _ = dedupe_archive(rows)
    assert merges == 1
    assert len(out) == 2
    kept = next(r for r in out if r["bhk"] == 3)
    assert set(kept["amenities"]) == {"A", "B"}
    assert kept["carpetArea"] == 1000


def test_find_match_prefers_richest_duplicate():
    archive = [
        _unit(amenities=[]),
        _unit(amenities=["Pool", "Gym", "Club"], carpetArea=1000),
    ]
    idx = find_match_index(archive, _unit(bathrooms=2))
    assert idx == 1


def test_dedupe_soft_typo_cluster():
    rows = [
        _unit(projectName="Gaurs Atulayam", amenities=["Pool"]),
        _unit(projectName="Gaurs Atulyam", carpetArea=1200, bathrooms=2),
    ]
    out, merges, _ = dedupe_archive(rows)
    assert merges == 1
    assert len(out) == 1
    assert out[0]["carpetArea"] == 1200
    assert out[0]["bathrooms"] == 2
    assert "Pool" in out[0]["amenities"]


if __name__ == "__main__":
    test_null_fill_merge()
    test_soft_project_match_and_archive_merge()
    test_soft_match_requires_locality()
    test_dedupe_archive_collapses_exact_dups()
    test_find_match_prefers_richest_duplicate()
    test_dedupe_soft_typo_cluster()
    print("ok")
