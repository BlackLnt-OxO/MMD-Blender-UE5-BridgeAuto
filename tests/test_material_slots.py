import pytest
from mmd_bridge.material_slots import validate_character_slots, character_material_namespaces


def test_material_slots_preserve_all_parts():
    records = [{'slot_name': f'M_{i}'} for i in range(40)]
    result = validate_character_slots([f'M_{i}' for i in reversed(range(40))], records)
    assert result['preserved'] and result['actual'] == 40
    with pytest.raises(RuntimeError, match='合并'):
        validate_character_slots(['M_0'], records)
    with pytest.raises(RuntimeError, match='重复'):
        validate_character_slots(['M_0'] * 40, records)


def test_ambiguous_source_materials_are_rejected():
    with pytest.raises(RuntimeError, match='重复身份'):
        validate_character_slots(['M_0'], [{'slot_name':'M_0'}, {'slot_name':'M_0'}])


def test_character_namespaces_are_stable_and_reject_ambiguous_ids():
    assert character_material_namespaces({'materials':[{'id':'single'}]}) == {}
    manifest={'characters':[{'materials':[{'id':'first','slot_name':'Body'}]},
                            {'materials':[{'id':'second','slot_name':'Body'}]}]}
    assert character_material_namespaces(manifest)=={'first':'C_0001','second':'C_0002'}
    manifest['characters'][1]['materials'][0]['id']='first'
    with pytest.raises(ValueError,match='重复材质身份'):
        character_material_namespaces(manifest)
