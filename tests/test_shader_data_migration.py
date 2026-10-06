import pytest
from mmd_bridge.scene_shader_data import restore_instance_values


def instance(name='Emitter',source='Tree',x=0,**extra):
    return dict(name=name,source_name=source,matrix_object=[[1,0,0,x],[0,1,0,0],[0,0,1,0],[0,0,0,1]],**extra)


def test_checkpoint_random_values_match_source_and_transform_not_enumeration_order():
    records=[instance(x=2,object_random=.2),instance(x=1,object_random=.1),instance(source='Other',object_random=.9)]
    assert restore_instance_values([instance(x=1),instance(x=2)],records)==[{'object_random':.1},{'object_random':.2}]


def test_missing_or_ambiguous_instances_never_guess_random_values():
    with pytest.raises(ValueError):restore_instance_values([instance(x=1)],[instance(x=2,object_random=.1)])
    with pytest.raises(ValueError):restore_instance_values([instance()],[instance(object_random=.1),instance(object_random=.2)])
