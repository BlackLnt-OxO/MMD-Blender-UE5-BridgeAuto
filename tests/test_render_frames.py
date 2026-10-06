import struct,zlib
from mmd_bridge.render_frames import png_complete,ledger,missing_ranges
from mmd_bridge.material_calibration import gpu_csv,calibration_key,save_measurement,lookup
import pytest


def write_png(path):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+
        chunk(b'IDAT',zlib.compress(b'\0\x80\x80\x80'))+chunk(b'IEND',b''))


def test_truncation_wrong_dimensions_and_missing_frames_block_acceptance(tmp_path):
    path=tmp_path/'MMDBridge_FinalImage_0001.png';write_png(path)
    assert png_complete(path,[1,1]) and not png_complete(path,[2560,1080])
    before=path.read_bytes();path.write_bytes(before[:-3]);assert not png_complete(path)
    path.write_bytes(before)
    receipt=ledger(tmp_path,{'start':0,'end':5,'step':1,'resolution':[1,1]})
    assert receipt['complete_frames']==[1]
    assert missing_ranges(receipt)==[[0,1],[2,5]]


def test_valid_crc_and_deflate_do_not_accept_incomplete_scanlines(tmp_path):
    path=tmp_path/'bad.png';write_png(path)
    raw=path.read_bytes();raw=raw[:16]+struct.pack('>II',2,1)+raw[24:]
    raw=raw[:29]+struct.pack('>I',zlib.crc32(raw[12:29])&0xffffffff)+raw[33:]
    path.write_bytes(raw)
    assert not png_complete(path,[2,1])


def test_cpu_timing_is_never_accepted_as_gpu_calibration(tmp_path):
    csv=tmp_path/'gpu.csv';csv.write_text('FrameTime,CPU/RenderThread\n'+('10,3\n'*64))
    with pytest.raises(ValueError,match='GPU'):gpu_csv(csv)
    csv.write_text('FrameTime,GPU/FrameTime\n'+('10,3\n'*64))
    result=gpu_csv(csv)
    assert result['gpu_ms']==3 and result['samples']==48
    csv.write_text('FrameTime,GPUTime\n'+('10,4\n'*64))
    assert gpu_csv(csv)['gpu_ms']==4


def test_calibration_key_invalidates_hardware_engine_graph_and_context():
    values=['GPU','UE5.8','v6','graph',{'triangles':100}]
    original=calibration_key(*values)
    for i in range(5):
        changed=values.copy();changed[i]='changed'
        assert calibration_key(*changed)!=original


def test_scene_gpu_calibration_excludes_mrq_idle_frames_before_warmup(tmp_path):
    path=tmp_path/'mrq.csv'
    path.write_text('GPUTime,GPU/Basepass\n'+('0.1,0\n'*100)+('50,2\n'*16)+('4,2\n'*32))
    assert gpu_csv(path)['gpu_ms']==.1
    measured=gpu_csv(path,scene_only=True)
    assert measured['gpu_ms']==4 and measured['samples']==32
    path.write_text('GPUTime\n'+('4\n'*64))
    with pytest.raises(ValueError,match='场景 GPU'):gpu_csv(path,scene_only=True)


def test_saved_calibration_rechecks_budget_on_resume(tmp_path):
    candidate=dict(id='nodes',compiled=True,semantic_valid=True,ray_tracing_valid=True,
                   gpu_ms=3,scene_peak_mb=6000,baked_bytes=0,import_ms=0)
    save_measurement(tmp_path,'key',{},[candidate],8192)
    assert lookup(tmp_path,'key',8192)['selected']=='nodes'
    assert lookup(tmp_path,'key',6000)['selected'] is None
