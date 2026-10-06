"""Blender Cycles data semantics, independent of UE random-number generators.

Compatible algorithm port from Blender Cycles util/hash.h and disjoint_set.h,
Copyright 2011-2022 Blender Foundation, Apache-2.0. See the notice in licenses.
"""
import struct

MASK=0xffffffff
GENERATED_UV_LAYERS = ('MMDBridge_Generated_XY', 'MMDBridge_Generated_ZW')


def has_generated_layers(mesh):
    """Both carriers are required; a partial layer is not a valid restore point."""
    return all(mesh.uv_layers.get(name) is not None for name in GENERATED_UV_LAYERS)


def instance_identity(instance):
    return (instance['name'],instance['source_name'],
            tuple(round(float(v),8) for row in instance['matrix_object'] for v in row))


def restore_instance_values(instances,source_records):
    """Migrate old checkpoints only when source identity and transform agree."""
    values={}
    for record in source_records:
        values.setdefault(instance_identity(record),set()).add(record['object_random'])
    restored=[]
    for instance in instances:
        matches=values.get(instance_identity(instance),set())
        if len(matches)!=1:
            raise ValueError('旧断点的实例身份缺失或不唯一，不能安全补齐对象随机值: '+instance['name'])
        restored.append({'object_random':next(iter(matches))})
    return restored


def f32(x):return struct.unpack('<f',struct.pack('<f',float(x)))[0]


def hash_uint(*values):
    if len(values) not in (1,2):raise ValueError('Expected one or two uints')
    a=b=c=(0xdeadbeef+(len(values)<<2)+13)&MASK
    a=(a+values[0])&MASK
    if len(values)==2:b=(b+values[1])&MASK
    rot=lambda n,k:((n<<k)|(n>>(32-k)))&MASK
    c=((c^b)-rot(b,14))&MASK
    a=((a^c)-rot(c,11))&MASK
    b=((b^a)-rot(a,25))&MASK
    c=((c^b)-rot(b,16))&MASK
    a=((a^c)-rot(c,4))&MASK
    b=((b^a)-rot(a,14))&MASK
    return ((c^b)-rot(b,24))&MASK


def uint_to_float(value):return f32(f32(value)*f32(1/f32(MASK)))


def object_random(name,instance_random_id=None):
    if instance_random_id is not None:return uint_to_float(instance_random_id&MASK)
    value=0
    for char in name.encode('utf-8'):
        value=(value*37+(char if char<128 else char-256))&MASK
    return uint_to_float(hash_uint(value,0))


def island_random(vertex_count,edges,face_vertices):
    """Match Cycles union order/rank and use a face value to avoid interpolation."""
    parents=list(range(vertex_count));ranks=[0]*vertex_count
    def root(x):
        r=x
        while parents[r]!=r:r=parents[r]
        while parents[x]!=r:
            previous=parents[x];parents[x]=r;x=previous
        return r
    for x,y in edges:
        x=root(int(x));y=root(int(y))
        if x==y:continue
        if ranks[x]<ranks[y]:x,y=y,x
        parents[y]=x
        if ranks[x]==ranks[y]:ranks[x]+=1
    cache={}
    result=[]
    for vertex in face_vertices:
        r=root(int(vertex))
        if r not in cache:cache[r]=uint_to_float(hash_uint(r))
        result.append(cache[r])
    return result


def ensure_island_layer(mesh):
    """A dedicated scalar carrier; the active vertex color is never overwritten."""
    import numpy as np
    name='MMDBridge_IslandRandom'
    found=mesh.uv_layers.get(name)
    if found:return {'uv_index':list(mesh.uv_layers).index(found),'component':0}
    packed=mesh.uv_layers.get('MMDBridge_Generated_ZW')
    if packed and mesh.get('_mmd_island_packed'):
        return {'uv_index':list(mesh.uv_layers).index(packed),'component':1}
    if not packed and len(mesh.uv_layers)>=8:raise ValueError('Blender UV 通道已用满，不能保留网格岛随机值烘焙输入')
    edges=np.empty(len(mesh.edges)*2,dtype=np.int32);mesh.edges.foreach_get('vertices',edges)
    starts=np.empty(len(mesh.polygons),dtype=np.int32);mesh.polygons.foreach_get('loop_start',starts)
    vertices=np.empty(len(mesh.loops),dtype=np.int32);mesh.loops.foreach_get('vertex_index',vertices)
    sizes=np.empty(len(mesh.polygons),dtype=np.int32);mesh.polygons.foreach_get('loop_total',sizes)
    values=np.repeat(np.asarray(island_random(len(mesh.vertices),edges.reshape(-1,2),vertices[starts]),dtype=np.float32),sizes)
    carrier=np.zeros((len(mesh.loops),2),dtype=np.float32)
    if packed:
        packed.data.foreach_get('uv',carrier.ravel());carrier[:,1]=values
        packed.data.foreach_set('uv',carrier.ravel());mesh['_mmd_island_packed']=True
        return {'uv_index':list(mesh.uv_layers).index(packed),'component':1}
    carrier[:,0]=values
    index=len(mesh.uv_layers);layer=mesh.uv_layers.new(name=name)
    layer.data.foreach_set('uv',carrier.ravel());return {'uv_index':index,'component':0}


def ensure_generated_layers(source,mesh):
    """A static mesh's original texture space, independent of transforms/units.

    Original coordinates survive verified deform-only operators. Topology and
    indices must also agree; equal vertex counts alone are insufficient.
    """
    import numpy as np
    names=GENERATED_UV_LAYERS
    if has_generated_layers(mesh):
        indices=[list(mesh.uv_layers).index(mesh.uv_layers[n]) for n in names]
        return [[indices[0],0],[indices[0],1],[indices[1],0]]
    if source.type!='MESH' or source.data.shape_keys:
        raise ValueError('Generated 的非网格 / Shape Keys ORCO 尚未验证')
    original=source.data
    a=np.empty(len(original.vertices)*3,dtype=np.float32);original.vertices.foreach_get('co',a)
    b=np.empty(len(mesh.vertices)*3,dtype=np.float32);mesh.vertices.foreach_get('co',b)
    active=[m for m in source.modifiers if m.show_render]
    evaluated_nodes = bool(active and all(m.type == 'NODES' for m in active))
    if evaluated_nodes:
        # Cycles uses the evaluated mesh's ORCO (or evaluated positions when
        # absent) and its texture space for a Geometry Nodes result. The host's
        # raw eight vertices are not the shader coordinate source. Qualified by
        # native Cycles comparisons of MyGO's three GN cymbal hosts.
        original=mesh
        a=np.empty(len(mesh.vertices)*3,dtype=np.float32)
        mesh.vertices.foreach_get('undeformed_co',a)
        mesh['_mmd_generated_provenance']='evaluated_nodes_texture_space_v1'
    elif a.shape!=b.shape:
        raise ValueError('Generated 原始顶点数量不对应；该修改器 ORCO 尚未验证')
    def indices(collection,name):
        width=2 if name=='vertices' else 1
        result=np.empty(len(collection)*width,dtype=np.int32);collection.foreach_get(name,result)
        return result
    for old,new,name in ((original.edges,mesh.edges,'vertices'),(original.loops,mesh.loops,'vertex_index'),
                         (original.polygons,mesh.polygons,'loop_total')):
        if len(old)!=len(new) or not np.array_equal(indices(old,name),indices(new,name)):
            raise ValueError('Generated 原始拓扑或顶点索引不对应')
    if not np.array_equal(a,b):
        # Native Cycles comparisons qualify these operators. Unknown topology
        # changing operators are never inferred from counts or preset names.
        verified={'DISPLACE','SIMPLE_DEFORM'}
        if not active or any(m.type not in verified for m in active):
            raise ValueError('Generated 形变修改器 ORCO 尚未验证: '+','.join(m.type for m in active))
    # These layers are also Cycles bake inputs. After conversion, only channels
    # actually referenced by the UE graph are packed into its four UV slots.
    if len(mesh.uv_layers)+2>8:raise ValueError('Blender UV 通道不足以保留 Generated 烘焙输入')
    center=np.asarray(original.texspace_location,dtype=np.float32)
    size=np.asarray(original.texspace_size,dtype=np.float32)
    if np.any(size<=0):raise ValueError('无效原始 texture space')
    generated=(a.reshape((-1,3))-center)/(size*2)+.5
    vertices=np.empty(len(mesh.loops),dtype=np.int32);mesh.loops.foreach_get('vertex_index',vertices)
    indices=[]
    for axes,name in zip(((0,1),(2,None)),names):
        index=len(mesh.uv_layers);layer=mesh.uv_layers.new(name=name)
        values=np.zeros((len(mesh.loops),2),dtype=np.float32);values[:,0]=generated[vertices,axes[0]]
        if axes[1] is not None:values[:,1]=generated[vertices,axes[1]]
        layer.data.foreach_set('uv',values.ravel());indices.append(index)
    return [[indices[0],0],[indices[0],1],[indices[1],0]]


def pin_generated(tree,mesh,seen=None,created=None):
    """Feed Cycles field caches the same preserved coordinates as UE functions."""
    if not has_generated_layers(mesh):
        # Keep the original Generated dependency visible to the failure policy.
        # Missing carriers must not be silently replaced by another source UV.
        return
    if not all(mesh.uv_layers.get('MMDBridge_Generated_'+a) for a in ('XY','ZW')):return
    seen={} if seen is None else seen;created=[] if created is None else created
    combine=None
    for node in list(tree.nodes):
        if node.type=='GROUP':
            key=node.node_tree.as_pointer()
            if key not in seen:
                clone=node.node_tree.copy();seen[key]=clone;created.append(clone)
                pin_generated(clone,mesh,seen,created)
            node.node_tree=seen[key]
        links=[];target=None
        if node.type=='TEX_COORD':links=list(node.outputs['Generated'].links)
        elif node.type in ('TEX_NOISE','TEX_VORONOI','TEX_CHECKER') and not node.inputs['Vector'].is_linked:target=node.inputs['Vector']
        if not links and target is None:continue
        if combine is None:
            combine=tree.nodes.new('ShaderNodeCombineXYZ')
            for axis,layer,component in (('X','XY',0),('Y','XY',1),('Z','ZW',0)):
                uv=tree.nodes.new('ShaderNodeUVMap');uv.uv_map='MMDBridge_Generated_'+layer
                split=tree.nodes.new('ShaderNodeSeparateXYZ');tree.links.new(uv.outputs[0],split.inputs[0])
                tree.links.new(split.outputs[component],combine.inputs[axis])
        for link in links:tree.links.new(combine.outputs[0],link.to_socket)
        if target is not None:tree.links.new(combine.outputs[0],target)
