from backend.config.device_registry import get_default_device_registry


def _registry_by_id():
    return {entry.id: entry for entry in get_default_device_registry()}


def test_showroom_scene_bindings_cover_animatable_nodes():
    registry = _registry_by_id()

    assert registry["ac_living_01"].scene_bindings["effect_nodes"] == ["effect1"]
    assert registry["ac_living_02"].scene_bindings["pick_nodes"] == ["ac2"]

    assert registry["curtain_living_01"].scene_bindings["panel_nodes"] == [
        {"node": "curtain01", "side": "left", "axis": "z"},
        {"node": "curtain02", "side": "right", "axis": "z"},
    ]
    assert registry["curtain_bedroom_02"].scene_bindings["pick_nodes"] == [
        "curtain2",
        "curtain01001",
        "curtain02001",
    ]
    assert registry["curtain_loft_02"].scene_bindings["panel_nodes"] == [
        {"node": "curtain01001", "side": "left", "axis": "z"},
        {"node": "curtain02001", "side": "right", "axis": "z"},
    ]
    assert registry["curtain_bedroom_01"].scene_bindings["panel_nodes"] == [
        {"node": "curtain01", "side": "left", "axis": "z"},
        {"node": "curtain02", "side": "right", "axis": "z"},
    ]

    assert registry["fan_living_01"].scene_bindings["rotor_nodes"] == ["fan01"]
    assert registry["fan_living_01"].scene_bindings["head_nodes"] == ["fan02"]
    assert registry["fan_living_01"].scene_bindings["rotor_axis"] == "x"
    assert set(registry["fan_living_01"].scene_bindings["rotor_nodes"]).isdisjoint(
        registry["fan_living_01"].scene_bindings["head_nodes"]
    )

    assert registry["camera_entry_01"].scene_bindings["cone_nodes"] == ["visualcone1"]
    assert registry["camera_bedroom_02"].scene_bindings["pick_nodes"] == ["cam2"]
    assert registry["camera_loft_02"].scene_bindings["cone_nodes"] == ["visualcone2"]


def test_showroom_curtains_default_to_fully_open_side_pose():
    registry = _registry_by_id()

    curtain_ids = [
        "curtain_living_01",
        "curtain_bedroom_01",
        "curtain_bedroom_02",
        "curtain_loft_01",
        "curtain_loft_02",
    ]

    for curtain_id in curtain_ids:
        assert registry[curtain_id].default_extra["open_percent"] == 100
