from mmd_bridge.core import Settings
from mmd_bridge.material_workflow import LEGACY_MAIN, SEMANTIC, workflow, legacy_channels, white_materials


def test_new_defaults_and_existing_job_compatibility():
    assert Settings().material_workflow == LEGACY_MAIN
    assert workflow({}) == SEMANTIC
    assert workflow({'material_workflow':LEGACY_MAIN}) == LEGACY_MAIN


def test_legacy_channels_keep_pbr_without_new_lobes():
    assert legacy_channels({'base_color':1,'normal':2,'coat':3,'transmission':4}) == {'base_color':1,'normal':2}


def test_failure_ledger_distinguishes_normal_white_and_review():
    manifest={'scene':{'materials':[
        {'id':'good','name':'White','strategy':'nodes','channels':{'base_color':{'op':'constant','value':[1,1,1,1]}},'requires_visual_review':True},
        {'id':'bad','name':'Paint','strategy':'nodes','channels':{},'material_error':'Unsupported coordinate'}]}}
    assert [r['id'] for r in white_materials(manifest, {})] == ['bad']
