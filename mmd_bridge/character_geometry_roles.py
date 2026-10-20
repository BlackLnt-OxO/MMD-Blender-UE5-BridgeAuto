"""Deterministic geometry purposes, independent of the physical shader type.

No model identities or slot numbers participate in automatic decisions. Callers
provide measured rest-pose evidence; absence of evidence means review, not all.
"""
import math
import re
import unicodedata

VERSION = 1
EXTENDED_VERSION = 2
LOCAL_ROLES = {'body', 'face', 'legwear', 'attached_layer'}
EXCLUDED = r'nails?|指甲|爪|eyebrow|eyelash|eyeline|(^|[^a-z])brows?([^a-z]|$)|眉|睫|teeth|tooth|歯|牙|tongue|舌|mouth|口腔|靴|鞋|boots?|shoes?|soles?|pocket|ポケット|口袋|buttons?|扣|釦|metal|silver|gold|金属|メタル|skirt|裙|スカート|cape|cloak|披风|headphone|耳机'
HAIR = r'hair|bangs|髪|髮|头发|頭髮'
EYES = r'iris|pupils?|sclera|eye.?white|eye.?light|eye.?shadow|白目|目白|^目$|眼白|^眼[0-9]*$|瞳|虹彩|眼影|髪影|hair.?shadow|脸红|臉紅|照れ|blush|表情|expressions?'
FACE = r'(^|[^a-z])(face[0-9]*|lips?)([^a-z]|$)|顔|脸|臉|唇|口紅|^面[0-9]*$'
BODY = r'(^|[^a-z])(body|skin|arms?)([^a-z]|$)|皮肤|皮膚|身体|身體|肌|^体$'
LEGWEAR = r'stockings?|hosiery|pantyhose|tights?|leggings?|garter|body.?suit|(^|[^a-z])suits?([^a-z]|$)|丝袜|絲襪|连裤袜|連褲襪|紧身|腿带|绑腿|ストッキング|タイツ|スーツ|ガーター'
PANTS = r'pants?|trousers?|shorts?|裤|褲|ショーツ'


def normalized(value):
    value = unicodedata.normalize('NFKC', str(value))
    return re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', value).casefold().strip()


def bone_role(name):
    value = normalized(name)
    compact = re.sub(r'[\s_.-]', '', value)
    if re.search(r'ik|ＩＫ|親|先|tip|twist|捩|補助|辅助', value):
        return None
    if re.search(r'ひざ|膝|knee|calf|lowerleg|shin', compact): return 'knee'
    if re.search(r'足首|ankle|foot|つま先|toe', compact): return 'foot'
    if re.search(r'^(左|右)足d?$|upperleg|thigh|^(left|right)leg$|^leg[lr]$', compact): return 'thigh'
    if re.search(r'頭|头|head|首|neck', compact): return 'head'
    if re.search(r'上半身|下半身|腰|spine|hips|pelvis|waist|胸|chest', compact): return 'trunk'
    if re.search(r'腕|ひじ|肘|肩|arm|elbow|shoulder', compact): return 'arm'
    if re.search(r'指|手首|finger|hand|wrist', compact): return 'hand'
    return None


def geometry_override(cfg, model_hash, index):
    return cfg.get('character_geometry_overrides', {}).get(model_hash, {}).get(str(index), {})


def validate_overrides(overrides):
    if not isinstance(overrides, dict): raise ValueError('几何用途覆盖必须是字典')
    for model, entries in overrides.items():
        if not isinstance(model, str) or not isinstance(entries, dict): raise ValueError('几何用途覆盖格式错误')
        for index, value in entries.items():
            if not str(index).isdigit() or not isinstance(value, dict): raise ValueError('几何用途槽位格式错误')
            if set(value)-{'local', 'host'}: raise ValueError('未知几何用途设置')
            if value.get('local', 'auto') not in ('auto', 'include', 'exclude'): raise ValueError('未知局部细分选择')
            host=value.get('host')
            if host is not None and (type(host) is not int or host < 0 or host == int(index)):
                raise ValueError('贴片宿主必须是其他原材质槽')


def purpose(record, evidence=None, override=None, *, extended=False):
    if extended:
        return extended_purpose(record, evidence, override)
    evidence = dict(evidence or {})
    override = override or {}
    mmd = record.get('mmd', {})
    names = [record.get('source_name', record.get('name', '')), mmd.get('name_j', ''), mmd.get('name_e', '')]
    # The original component field has priority over generic exporter labels.
    name = next((normalized(n) for n in names if n), '')
    pbr = record.get('character_material', {}).get('pbr', record.get('pbr', {}))
    kind = pbr.get('type', 'generic')
    role = 'unknown'; reasons = []
    robot = re.search(r'robot|机械|機械|机器人|機器人', normalized(record.get('model_name') or evidence.get('model_name_context', '')))
    rig = evidence.get('humanoid_rig', False)
    if re.search(EXCLUDED, name):
        role='excluded'; reasons.append('原部件语义属于指甲、五官内部、鞋、硬件或宽松附件')
    elif re.search(HAIR, name) or kind=='hair':
        role='excluded'; reasons.append('头发不参与局部组')
    elif re.search(EYES, name) or kind in ('iris','sclera','eye_highlight','eye_shadow','lash','mouth','teeth'):
        role='excluded'; reasons.append('眼层或口腔内部不参与局部组')
    elif evidence.get('attached_host') is not None:
        role='attached_layer'; reasons.append('通过共面几何及变形兼容性确定宿主')
    elif evidence.get('candidate_hosts'):
        reasons.append('存在共面宿主候选 '+str(evidence['candidate_hosts'])+'；导出时须验证透明度、蒙皮及全部形态键')
    elif evidence.get('mixed_purposes'):
        reasons.append('整槽包含冲突的几何用途，需手动确认')
    elif not rig:
        reasons.append('缺少可验证的头部与腿部骨架证据，不能只凭名称选择几何')
    elif robot and re.search(BODY, name):
        reasons.append('机械身体不能按人类身体的名称及蒙皮自动纳入')
    elif re.search(FACE, name) or pbr.get('surface_role')=='lips':
        if kind in ('metal','plastic','glass','gem','rubber'):
            reasons.append('面部名称与硬质材质证据冲突')
        elif evidence.get('head_weight_ratio', 0) >= .65:
            role='face'; reasons.append('面部语义与头骨权重一致')
        else: reasons.append('面部名称与头骨权重证据不一致')
    elif re.search(BODY, name) and not robot:
        if kind in ('metal','plastic','glass','gem','rubber'):
            reasons.append('身体名称与硬质材质证据冲突')
        elif evidence.get('body_weight_ratio', 0) >= .6:
            role='body'; reasons.append('身体语义与人形骨架权重一致；未按skin类型直接选择')
        else: reasons.append('身体名称缺少对应蒙皮证据')
    elif re.search(LEGWEAR, name) or re.search(PANTS, name):
        strong = bool(re.search(LEGWEAR, name))
        near = evidence.get('near_leg_ratio', 0)
        weighted = evidence.get('leg_weight_ratio', 0)
        if weighted >= .25 and near >= (.35 if strong else .7):
            role='legwear'; reasons.append('贴身衣物语义、腿骨权重及相对腿轴位置一致；按整槽处理')
        else: reasons.append('衣物名称不足以证明贴腿，需核对骨骼及几何距离')
    elif kind in ('metal','plastic','glass','gem','rubber'):
        role='excluded'; reasons.append('硬质物理材质不自动作为贴身软层')
    else:
        reasons.append('名称不足；物理材质类型不能单独确定几何用途')
    selected = role in LOCAL_ROLES
    if override.get('local') in ('include','exclude'):
        selected=override['local']=='include'; reasons.append('采用用户明确的几何选择')
    return {'version':VERSION,'role':role,'local_selected':selected,
            'method':'manual_override' if override.get('local') in ('include','exclude') else 'semantic_rig_geometry',
            'needs_review':role=='unknown' and override.get('local','auto')=='auto',
            'reasons':reasons,'evidence':evidence,'host_override':override.get('host')}


def rest_evidence(points, weights, bones, *, extended=False):
    """Scale independent evidence in any consistent rest-coordinate system."""
    roles={i:bone_role(b.get('original', b.get('name',''))) for i,b in enumerate(bones)}
    available=set(roles.values())
    rig='head' in available and 'thigh' in available and 'knee' in available
    sums={'head':0.,'body':0.,'leg':0.}; near=0
    leg_segments=[]
    for i,b in enumerate(bones):
        if roles[i] not in ('thigh','knee'):continue
        p=b.get('position');end=b.get('end')
        if p is not None and end is not None and math.dist(p,end)>1e-8:leg_segments.append((p,end))
    for point, memberships in zip(points, weights):
        total=sum(max(0.,w) for _,w in memberships) or 1.
        for index,w in memberships:
            role=roles.get(index)
            sums['head']+=w/total*(role=='head')
            sums['body']+=w/total*(role in ('head','trunk','arm','hand','thigh','knee','foot'))
            sums['leg']+=w/total*(role in ('thigh','knee','foot'))
        for a,b in leg_segments:
            direction=[y-x for x,y in zip(a,b)];length2=sum(x*x for x in direction)
            t=sum((x-y)*d for x,y,d in zip(point,a,direction))/length2
            if -.15<=t<=1.15:
                nearest=[x+max(0.,min(1.,t))*d for x,d in zip(a,direction)]
                if math.dist(point,nearest)<=math.sqrt(length2)*.35:
                    near+=1;break
    count=max(1,len(points))
    result = {'humanoid_rig':rig,'head_weight_ratio':sums['head']/count,
            'body_weight_ratio':sums['body']/count,'leg_weight_ratio':sums['leg']/count,
            'near_leg_ratio':near/count,'vertices_measured':len(points),
             'bone_roles':sorted(r for r in available if r)}
    if extended:
        for label, wanted in (('trunk', {'trunk'}), ('arm', {'arm'}), ('hand', {'hand'})):
            result[label+'_weight_ratio'] = sum(
                sum(w for i,w in memberships if roles.get(i) in wanted) /
                (sum(w for _,w in memberships) or 1.) for memberships in weights) / count
        # Actual rest-bone segments provide a scale-independent spatial check.
        positions = [b['position'] for b in bones if b.get('position') is not None]
        height = max((p[2] for p in positions), default=0.)-min((p[2] for p in positions), default=0.)
        segments = [(bones[b['parent']]['position'], b['position']) for i,b in enumerate(bones)
                    if roles.get(i) and 0 <= b.get('parent',-1) < len(bones)
                    and roles.get(b['parent']) and b.get('position') is not None]
        close = 0
        for point in points:
            for a,b in segments:
                direction = [y-x for x,y in zip(a,b)]; length2 = sum(x*x for x in direction)
                if length2 < 1e-12: continue
                t = max(0., min(1., sum((x-y)*d for x,y,d in zip(point,a,direction))/length2))
                if math.dist(point, [x+t*d for x,d in zip(a,direction)]) <= height*.14:
                    close += 1; break
        result['near_body_ratio'] = close/count
    return result


NECK = r'(^|[^a-z])(neck)([^a-z]|$)|脖|颈|頸|^首$'
LIPS = r'(^|[^a-z])lips?([^a-z]|$)|唇|口紅'
FITTED = r'tight|body.?suit|leotard|under.?shirt|compression|skin.?suit|紧身|緊身|贴身|貼身|レオタード|インナー'
GLOVES = r'gloves?|手套|手袋|グローブ'


def extended_purpose(record, evidence=None, override=None):
    evidence = dict(evidence or {}); override = override or {}
    mmd = record.get('mmd', {})
    name = next((normalized(n) for n in (record.get('source_name',record.get('name','')),
                                         mmd.get('name_j',''),mmd.get('name_e','')) if n), '')
    pbr = record.get('character_material',{}).get('pbr',record.get('pbr',{}))
    result = purpose(record, evidence, override)
    result['version'] = EXTENDED_VERSION
    optional = 'neck' if re.search(NECK,name) else 'lips' if re.search(LIPS,name) or pbr.get('surface_role')=='lips' else None
    result['optional_region'] = optional
    result['anatomical_region'] = optional or ('face' if re.search(FACE,name) else None)
    if override.get('local','auto') != 'auto': return result
    hard = pbr.get('type') in ('metal','plastic','glass','gem','rubber')
    mixed = bool(evidence.get('mixed_purposes'))
    rig = evidence.get('humanoid_rig',False)
    role = None; reason = None
    if optional and not re.search(r'necklace|项链|項鏈|首飾|collar|领|襟|眉|睫|牙|歯|teeth',name):
        valid = rig and not hard and not mixed and evidence.get('head_weight_ratio',0)+evidence.get('trunk_weight_ratio',0) >= .6
        role = ('neck' if optional=='neck' else 'face') if valid else 'unknown'
        reason = '颈部／嘴唇语义与头颈蒙皮证据一致' if valid else '颈部／嘴唇未确认，仅日志提示'
    elif result['role']=='unknown' and not hard and not mixed and not evidence.get('candidate_hosts'):
        glove = bool(re.search(GLOVES,name)); fitted = bool(re.search(FITTED,name))
        if glove or fitted:
            weight = evidence.get('hand_weight_ratio',0)+evidence.get('arm_weight_ratio',0) if glove else evidence.get('body_weight_ratio',0)
            valid = rig and weight >= .6 and evidence.get('near_body_ratio',0) >= .6
            role = 'close_fitting' if valid else 'unknown'
            reason = '贴身软层语义、对应骨骼及空间证据一致' if valid else '贴身软层证据不足，需手动选择'
    if role is not None:
        result.update(role=role,local_selected=role in ('neck','face','close_fitting'),
                      needs_review=role=='unknown',reasons=[reason])
    return result
