"""Verified PNG frame receipts and contiguous resume ranges, independent of UE."""
import re,struct,zlib
from pathlib import Path
from .core import file_hash


def png_complete(path,resolution=None):
    """Check chunk CRCs, a complete deflate stream and a final IEND chunk."""
    try:
        raw=Path(path).read_bytes()
        if raw[:8]!=b'\x89PNG\r\n\x1a\n':return False
        cursor=8;payload=[];size=None;row_bytes=None
        while cursor<len(raw):
            length=struct.unpack_from('>I',raw,cursor)[0];kind=raw[cursor+4:cursor+8]
            end=cursor+8+length
            if end+4>len(raw):return False
            data=raw[cursor+8:end]
            if zlib.crc32(kind+data)&0xffffffff!=struct.unpack_from('>I',raw,end)[0]:return False
            if kind==b'IHDR':
                if size or cursor!=8 or length!=13:return False
                width,height,depth,color,compression,filtering,interlace=struct.unpack('>IIBBBBB',data)
                channels={0:1,2:3,3:1,4:2,6:4}.get(color)
                depths={0:(1,2,4,8,16),2:(8,16),3:(1,2,4,8),4:(8,16),6:(8,16)}
                if not width or not height or channels is None or depth not in depths[color]:return False
                if compression or filtering or interlace:return False
                size=[width,height];row_bytes=(width*channels*depth+7)//8
            if kind==b'IDAT':payload.append(data)
            cursor=end+4
            if kind==b'IEND':
                if cursor!=len(raw) or not payload or not size or (resolution and size!=resolution):return False
                decoder=zlib.decompressobj();pixels=decoder.decompress(b''.join(payload));decoder.flush()
                return (len(pixels)==(row_bytes+1)*size[1] and decoder.eof and not decoder.unused_data
                        and all(pixels[i] in range(5) for i in range(0,len(pixels),row_bytes+1)))
    except (OSError,ValueError,struct.error,zlib.error):pass
    return False


def ledger(output,contract):
    frames={};invalid=[]
    expected=set(range(contract['start'],contract['end'],contract.get('step',1)))
    for path in sorted(Path(output).rglob('MMDBridge_*.png')):
        match=re.fullmatch(r'MMDBridge_(.+)_(-?\d+)\.png',path.name)
        if not match or int(match[2]) not in expected:continue
        if not png_complete(path,contract.get('resolution')):invalid.append(str(path));continue
        frame=int(match[2]);key=(frame,match[1])
        if key in frames:invalid.append(str(path));continue
        frames[key]={'path':str(path),'sha256':file_hash(path)}
    passes=contract.get('passes',['FinalImage'])
    complete=[f for f in sorted(expected) if all((f,p) in frames for p in passes)]
    return {'complete_frames':complete,'missing_frames':sorted(expected-set(complete)),
            'invalid_files':invalid,'files':[dict(frame=f,render_pass=p,**v) for (f,p),v in frames.items()]}


def missing_ranges(receipt,step=1):
    ranges=[]
    for frame in receipt['missing_frames']:
        if ranges and frame==ranges[-1][1]:ranges[-1][1]=frame+step
        else:ranges.append([frame,frame+step])
    return ranges
