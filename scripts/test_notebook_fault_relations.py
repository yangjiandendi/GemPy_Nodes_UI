from __future__ import annotations

import sys
import types
from enum import Enum
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.notebook_standalone_runtime import configure_structural_frame


class Relation(Enum):
    ERODE = "ERODE"
    FAULT = "FAULT"


class Group:
    def __init__(self, name: str):
        self.name = name
        self.structural_relation = Relation.ERODE
        self.elements = []


class StructuralFrame:
    def __init__(self):
        self.structural_groups = [
            Group("Fault_Series"),
            Group("Upper_Series"),
            Group("Folded_Metamorphic_Series"),
        ]
        self._fault_relations = None

    @property
    def fault_relations(self):
        if self._fault_relations is None:
            raise ValueError("fault relations getter called before matrix assignment")
        return self._fault_relations

    @fault_relations.setter
    def fault_relations(self, value):
        self._fault_relations = np.asarray(value)


class Transform:
    def apply_anisotropy(self, _value):
        pass


class GeoModel:
    def __init__(self):
        self.structural_frame = StructuralFrame()
        self.input_transform = Transform()


def install_fake_gempy() -> None:
    module = types.ModuleType("gempy")
    module.data = types.SimpleNamespace(
        GlobalAnisotropy=types.SimpleNamespace(NONE="NONE"),
        StackRelationType=Relation,
    )

    def map_stack_to_surfaces(*, gempy_model, mapping_object):
        group_by_name = {group.name: group for group in gempy_model.structural_frame.structural_groups}
        gempy_model.structural_frame.structural_groups = [group_by_name[name] for name in mapping_object]

    module.map_stack_to_surfaces = map_stack_to_surfaces
    module.remove_structural_group_by_name = lambda **_kwargs: None
    sys.modules["gempy"] = module


def configured_model(fault_config):
    mapping = {
        "Fault_Series": ["shear_zone_fault"],
        "Upper_Series": ["upper_schist"],
        "Folded_Metamorphic_Series": ["amphibolite_band", "lower_gneiss"],
    }
    groups = [
        {"index": 0, "name": "Fault_Series", "relation": "FAULT"},
        {"index": 1, "name": "Upper_Series", "relation": "ERODE"},
        {"index": 2, "name": "Folded_Metamorphic_Series", "relation": "ERODE"},
    ]
    return configure_structural_frame(
        GeoModel(),
        mapping_json=mapping,
        groups_json=groups,
        remove_default_formation=False,
        fault_relations_json=fault_config,
    )


def assert_matrix(config) -> None:
    model = configured_model(config)
    expected = np.asarray([[0, 1, 1], [0, 0, 0], [0, 0, 0]])
    np.testing.assert_array_equal(model.structural_frame._fault_relations, expected)


def main() -> None:
    install_fake_gempy()
    assert_matrix({
        "enabled": True,
        "relations": [
            {"from": "Fault_Series", "to": "Upper_Series", "active": True},
            {"from": "Fault_Series", "to": "Folded_Metamorphic_Series", "active": True},
        ],
    })
    assert_matrix({
        "enabled": True,
        "relations": [
            {"source": "Fault_Series", "target": "Upper_Series", "value": True},
            {"source": "Fault_Series", "target": "Folded_Metamorphic_Series", "value": True},
        ],
    })
    assert_matrix({"enabled": True, "matrix": [[0, 1, 1], [0, 0, 0], [0, 0, 0]]})
    print("Notebook fault-relation matrix tests passed.")


if __name__ == "__main__":
    main()
