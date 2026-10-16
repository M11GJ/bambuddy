"""Slicer rejections that members hit on the SwapMod printer get a translation key."""

from backend.app.api.routes.library import _slicer_rejection_message

PREFIX = "Slicer API error (500): Slicing failed with error from slicer: "


def test_empty_plate_gets_outside_bed_key():
    text = (
        PREFIX + "One of the plate is empty or has no object fully inside it. Please check that the 3mf "
        "contains no empty plate in Bambu Studio before uploading.: Slicer process failed (exit code 206)"
    )
    msg = _slicer_rejection_message(text)
    assert msg.startswith("[swap.slice.outsideBed] One of the plate is empty")


def test_exclusion_area_conflict_gets_exclusion_key():
    text = (
        PREFIX + "Object conflicts were detected. Please verify the slicing of all plates in Bambu Studio "
        "before uploading.: Slicer process failed (exit code 192)\nstderr: outputs/a.stl is too close to "
        "exclusion area, there may be collisions when printing."
    )
    msg = _slicer_rejection_message(text)
    assert msg.startswith("[swap.slice.exclusionArea] Object conflicts were detected")


def test_other_object_conflict_is_left_alone():
    text = PREFIX + "Object conflicts were detected. Please verify.: Slicer process failed (exit code 192)"
    assert _slicer_rejection_message(text) == "Object conflicts were detected. Please verify."


def test_non_rejection_is_still_none():
    assert _slicer_rejection_message("Slicer CLI failed (500): Failed to slice the model") is None
