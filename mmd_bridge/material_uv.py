"""Compact only UV channels referenced by the converted graph, including functions."""
import copy
from .material_graph import VERSION,signature,semantic_ir


def used_channels(records,library,include_base=True):
    used={0} if include_base else set();visited=set()
    def visit(value):
        if isinstance(value,dict):
            if value.get('op')=='uv':used.add(int(value.get('index',0)))
            if value.get('op')=='mesh_scalar':used.add(int(value['uv_index']))
            identity=value.get('function') if value.get('op')=='function_call' or value.get('kind')=='function_call' else None
            if identity and identity not in visited:
                visited.add(identity);visit(library.definitions[identity]['body'])
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    for record in records:
        visit(record.get('channels',{}));visit(record.get('surface_graph'))
    return sorted(used)


def relocate_record(record,mapping,library):
    """Definitions get new semantic identities when their channel indices change."""
    functions={};changed=False
    def rewrite(value):
        nonlocal changed
        if isinstance(value,dict):
            result={k:rewrite(v) for k,v in value.items()}
            key='index' if result.get('op')=='uv' else 'uv_index' if result.get('op')=='mesh_scalar' else None
            if key:
                old=int(result.get(key,0));result[key]=mapping[old];changed|=old!=mapping[old]
            if result.get('op')=='function_call' or result.get('kind')=='function_call':
                old=result['function']
                if old not in functions:
                    definition=library.definitions[old]
                    body=rewrite(definition['body'])
                    identity=signature({'version':VERSION,'body':semantic_ir(body),
                        'inputs':[i['type'] for i in definition['inputs']],'output_type':definition['output_type']})
                    library.definitions.setdefault(identity,dict(definition,id=identity,body=body))
                    functions[old]=identity
                result['function']=functions[old];changed|=old!=result['function']
            return result
        if isinstance(value,list):return [rewrite(v) for v in value]
        return value
    output=copy.deepcopy(record)
    output['channels']=rewrite(output.get('channels',{}))
    if output.get('surface_graph'):output['surface_graph']=rewrite(output['surface_graph'])
    if not changed:return record
    output['id']=signature([record['id'],'uv_layout_v1',sorted(mapping.items())])[:16]
    output['function_definitions']=library.reachable({'channels':output['channels'],'surface_graph':output.get('surface_graph')})
    output['uv_layout']={str(k):v for k,v in mapping.items()}
    return output
