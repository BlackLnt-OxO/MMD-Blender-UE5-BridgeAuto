import copy
import struct
from mmd_bridge.blender_hair_seams import seam_pairs


def fixture():
    points=[(0,0,0),(1,0,0),(1,0,0),(0,0,0)]
    edges=[{'vertices':(0,1),'normal':(0,0,1),'normals':[(0,0,1)]*2,'uvs':[(0,0),(1,0)]},
           {'vertices':(2,3),'normal':(0,0,1),'normals':[(0,0,1)]*2,'uvs':[(1,1),(0,1)]}]
    return points,edges,['same']*4


def test_only_exact_smooth_uv_seams_with_identical_deformation_join():
    p,e,s=fixture();pairs,rejected=seam_pairs(p,e,s)
    assert pairs==[((0,3),(1,2))] and not rejected
    p[3]=(0,0,1e-8)
    assert not seam_pairs(p,e,s)[0]


def test_different_morphs_or_skin_payload_keep_independent_motion():
    p,e,s=fixture();s[3]='different morph/skin'
    assert seam_pairs(p,e,s)[1]=={'deformation':1}


def test_opposite_sheets_authored_sharp_and_same_uv_stay_split():
    p,e,s=fixture();e[1]['normal']=(0,0,-1)
    assert not seam_pairs(p,e,s)[0]
    p,e,s=fixture();e[0]['protected']=True
    assert seam_pairs(p,e,s)[1]=={'authored_protection':1}
    p,e,s=fixture();e[1]['uvs']=[(1,0),(0,0)]
    assert seam_pairs(p,e,s)[1]=={'not_a_uv_seam':1}
    p,e,s=fixture();e[1]['normals']=[(0,1,0)]*2
    assert seam_pairs(p,e,s)[1]=={'authored_normals':1}


def test_ambiguous_coincident_edges_are_reviewed_instead_of_guessed():
    p,e,s=fixture();e.append(copy.deepcopy(e[1]))
    assert seam_pairs(p,e,s)==([] ,{'ambiguous_edges':1})


def test_uv_morph_seams_stay_independent():
    p,e,s=fixture();e[1]['uv_morph']=True
    assert seam_pairs(p,e,s)==([] ,{'uv_morph':1})


def test_optional_pmx_uv_collection_preserves_default_parser_and_unsigned_ids():
    from mmd_bridge.pmx_binary import ByteReader
    from mmd_bridge.pmx_model import PMXHeader
    from mmd_bridge.pmx_reader import _read_morphs
    raw=struct.pack('<i',1)+struct.pack('<i',0)*2+bytes([1,3])+struct.pack('<i',1)+bytes([200])+struct.pack('<4f',0,0,0,0)
    header=PMXHeader(vertex_index_size=1)
    reader=ByteReader(raw);reader.text_encoding='utf-8'
    assert _read_morphs(reader,header) is None and reader.pos==len(raw)
    reader=ByteReader(raw);reader.text_encoding='utf-8'
    assert _read_morphs(reader,header,collect_uv_vertices=True)=={200} and reader.pos==len(raw)
