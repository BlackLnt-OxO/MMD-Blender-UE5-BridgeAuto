"""Signed distance and full-sphere atlas layout, independent of DCC APIs."""
import math


def direction_grid():
    """16 yaw columns, 9 pitch rows; the two pole masks are shared."""
    return [(column, row, column*math.tau/16, (row-4)*math.pi/8)
            for row in range(9) for column in range(16)
            if row not in (0, 8) or column == 0]


def light_vector(yaw, pitch):
    return (math.sin(yaw)*math.cos(pitch), -math.cos(yaw)*math.cos(pitch), math.sin(pitch))


def atlas_neighbors(yaw, pitch):
    x = (yaw % math.tau)*16/math.tau
    y = max(0., min(8., pitch*8/math.pi+4))
    x0, y0 = int(math.floor(x)), min(7, int(math.floor(y)))
    fx, fy = x-x0, y-y0
    return [((x0 % 16, y0), (1-fx)*(1-fy)),
            (((x0+1) % 16, y0), fx*(1-fy)),
            ((x0 % 16, y0+1), (1-fx)*fy),
            (((x0+1) % 16, y0+1), fx*fy)]


def edt_1d(values):
    """Lower envelope of squared parabolas (exact Euclidean distance)."""
    sites = [i for i, v in enumerate(values) if math.isfinite(float(v))]
    if not sites:
        return [math.inf]*len(values)
    centers = [sites[0]]
    edges = [-math.inf, math.inf]
    for q in sites[1:]:
        while True:
            p = centers[-1]
            s = ((float(values[q])+q*q)-(float(values[p])+p*p))/(2*(q-p))
            if s > edges[-2]:
                break
            centers.pop(); edges.pop(-2)
        centers.append(q); edges.insert(-1, s)
    result, k = [], 0
    for q in range(len(values)):
        while edges[k+1] < q:
            k += 1
        p = centers[k]
        result.append((q-p)*(q-p)+float(values[p]))
    return result


def signed_distance(mask):
    # NumPy belongs to Blender, not the desktop interpreter.
    import numpy as np
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2 or not mask.size:
        raise ValueError('SDF 遮罩必须为非空二维数组')
    height, width = mask.shape
    extent = math.hypot(width, height)
    if mask.all():
        return np.full(mask.shape, extent, dtype=np.float32)
    if not mask.any():
        return np.full(mask.shape, -extent, dtype=np.float32)
    def distance_to(features):
        value = np.where(features, 0., math.inf)
        for y in range(height):
            value[y, :] = edt_1d(value[y, :])
        for x in range(width):
            value[:, x] = edt_1d(value[:, x])
        return np.sqrt(value)
    return (distance_to(~mask)-distance_to(mask)).astype(np.float32)


def threshold_from_distances(fields, angles, interpolation='linear'):
    """Interpolate the first lit-to-shadow zero crossing into a 0..1 angle."""
    import numpy as np
    fields = np.asarray(fields, dtype=np.float32)
    angles = np.asarray(angles, dtype=np.float32)
    if (fields.ndim != 3 or not fields.size or fields.shape[0] != len(angles) or len(angles) < 2
            or not np.isfinite(fields).all() or not np.isfinite(angles).all()
            or angles[0] != 0 or angles[-1] != 1 or (np.diff(angles) <= 0).any()):
        raise ValueError('单张 SDF 需要有限距离场及从 0 到 1 递增的角度')
    if interpolation not in ('linear','pchip'):
        raise ValueError('不支持的单张 SDF 插值方式')
    slopes = None
    if interpolation == 'pchip':
        h = np.diff(angles)[:,None,None]
        secants = np.diff(fields,axis=0)/h
        slopes = np.empty_like(fields)
        slopes[0],slopes[-1] = secants[0],secants[-1]
        if len(angles)>2:
            same_sign = ((secants[:-1]>0)&(secants[1:]>0))|((secants[:-1]<0)&(secants[1:]<0))
            w1,w2 = 2*h[1:]+h[:-1],h[1:]+2*h[:-1]
            inverse = np.divide(w1,secants[:-1],out=np.zeros_like(secants[:-1]),where=same_sign)
            inverse += np.divide(w2,secants[1:],out=np.zeros_like(secants[1:]),where=same_sign)
            slopes[1:-1] = np.divide(w1+w2,inverse,out=np.zeros_like(inverse),where=same_sign)
            for endpoint,first,second in ((0,0,1),(-1,-1,-2)):
                d = ((2*h[first]+h[second])*secants[first]-h[first]*secants[second])/(h[first]+h[second])
                d = np.where(np.sign(d)!=np.sign(secants[first]),0,d)
                limit = (np.sign(secants[first])!=np.sign(secants[second]))&(np.abs(d)>3*np.abs(secants[first]))
                slopes[endpoint] = np.where(limit,3*secants[first],d)
    result = np.ones(fields.shape[1:], dtype=np.float32)
    result[fields[0] <= 0] = 0
    pending = fields[0] > 0
    relit = np.zeros(result.shape, dtype=bool)
    for index in range(len(angles)-1):
        start, end = fields[index], fields[index+1]
        crossing = pending & (end <= 0)
        denominator = start-end
        fraction = np.divide(start, denominator, out=np.zeros_like(start), where=denominator != 0)
        if slopes is not None and crossing.any():
            a,b = start[crossing],end[crossing]
            interval = angles[index+1]-angles[index]
            m0,m1 = slopes[index][crossing]*interval,slopes[index+1][crossing]*interval
            c3,c2 = 2*a-2*b+m0+m1,-3*a+3*b-2*m0-m1
            lower,upper = np.zeros_like(a),np.ones_like(a)
            for _ in range(20):
                t = (lower+upper)*.5
                value = ((c3*t+c2)*t+m0)*t+a
                lower = np.where(value>0,t,lower); upper = np.where(value>0,upper,t)
            fraction[crossing] = (lower+upper)*.5
        result[crossing] = (angles[index]+fraction*(angles[index+1]-angles[index]))[crossing]
        pending[crossing] = False
        relit |= (start <= 0) & (end > 0)
    # shortcut: one threshold cannot encode re-entry into light; retain its review mask.
    return result, relit


def smooth_threshold(value, coverage, sigma):
    """Blur only supported face pixels so eye holes do not darken their borders."""
    import numpy as np
    value,coverage = np.asarray(value,dtype=np.float32),np.asarray(coverage,dtype=bool)
    if value.ndim != 2 or not value.size or value.shape != coverage.shape or not np.isfinite(value).all():
        raise ValueError('SDF 平滑需要有限阈值及同尺寸覆盖遮罩')
    if not math.isfinite(sigma) or not 0 <= sigma <= 16:
        raise ValueError('SDF smooth_sigma 需为 0 至 16 的有限数值')
    if sigma == 0:
        return value.copy()
    radius = math.ceil(3*sigma)
    offsets = np.arange(-radius,radius+1)
    weights = np.exp(-.5*(offsets/sigma)**2);weights /= weights.sum()
    numerator,denominator = value*coverage,coverage.astype(np.float32)
    for axis in (0,1):
        padding = [(0,0),(0,0)];padding[axis] = (radius,radius)
        def convolve(array):
            padded = np.pad(array,padding,mode='edge')
            result = np.zeros_like(array)
            for index,weight in enumerate(weights):
                slices = [slice(None),slice(None)];slices[axis] = slice(index,index+array.shape[axis])
                result += padded[tuple(slices)]*weight
            return result
        numerator,denominator = convolve(numerator),convolve(denominator)
    return np.divide(numerator,denominator,out=np.zeros_like(numerator),where=denominator>0)


def encode_threshold(angle, contrast):
    """Same monotone encoding on texture and light angle preserves the cutoff."""
    import numpy as np
    angle = np.asarray(angle,dtype=np.float32)
    if not np.isfinite(angle).all() or not math.isfinite(contrast) or not 0 <= contrast <= 12:
        raise ValueError('SDF 编码需要有限角度及 0 至 12 的 contrast')
    if contrast <= .001:
        return angle.copy()
    return (.5+.5*np.tanh(contrast*(angle-.5))/math.tanh(contrast*.5)).astype(np.float32)


def nose_cutouts(positions, coverage):
    """Fit a conservative nose-tip candidate in the standard PMX head frame."""
    import numpy as np
    positions,coverage = np.asarray(positions,dtype=np.float32),np.asarray(coverage,dtype=bool)
    if positions.shape != (*coverage.shape,3) or not coverage.any() or not np.isfinite(positions).all():
        raise ValueError('鼻部定位缺少有效表面坐标')
    points = positions[coverage]
    lo,hi = points.min(axis=0),points.max(axis=0)
    width,height = float(hi[0]-lo[0]),float(hi[2]-lo[2])
    if min(width,height)<1e-5:
        raise ValueError('鼻部定位的面部范围退化')
    center = (hi[0]+lo[0])/2
    candidates = (np.abs(points[:,0]-center)<.14*width)&(points[:,2]>lo[2]+.22*height)&(points[:,2]<lo[2]+.6*height)
    if not candidates.any():
        raise ValueError('没有鼻尖候选表面')
    nose = points[candidates][np.argmin(points[candidates,1])]
    cheeks = (np.abs(points[:,0]-center)>.1*width)&(np.abs(points[:,0]-center)<.3*width)&(np.abs(points[:,2]-nose[2])<.08*height)
    prominence = float(np.median(points[cheeks,1])-nose[1])/width if cheeks.any() else 0.
    if prominence<.015:
        raise ValueError('鼻尖突出程度不足，需明确鼻部位置')
    weights = []
    for side in (-1,1):
        x,z = nose[0]+side*.008*width,nose[2]+.010*height
        radius = ((positions[...,0]-x)/(.012*width))**2+((positions[...,2]-z)/(.026*height))**2
        weight = np.clip((1-radius)/.25,0,1)*coverage*(positions[...,1]<nose[1]+.06*width)
        if (weight>.5).sum()<2:
            raise ValueError('鼻部亮区的有效纹素不足')
        weights.append(weight.astype(np.float32))
    # shortcut: this geometric candidate needs review on flat or unusual anime noses.
    return *weights,{'status':'generated','position':nose.tolist(),'prominence_relative_width':prominence}


def apply_lit_cutout(threshold, weight, end_angle):
    import numpy as np
    threshold,weight = np.asarray(threshold,dtype=np.float32),np.asarray(weight,dtype=np.float32)
    if threshold.shape!=weight.shape or not np.isfinite(threshold).all() or not np.isfinite(weight).all() or not 0<=end_angle<=1:
        raise ValueError('鼻部亮区需要同尺寸遮罩及有效终止角度')
    return threshold+np.clip(weight,0,1)*np.maximum(0,end_angle-threshold)


def cheek_triangle_cutouts(positions, coverage, nose_info):
    """Rounded cheek triangles anchored to the detected nose, in PMX space."""
    import numpy as np
    positions,coverage = np.asarray(positions,dtype=np.float32),np.asarray(coverage,dtype=bool)
    nose = np.asarray(nose_info.get('position',[]),dtype=np.float32)
    if (positions.shape != (*coverage.shape,3) or not coverage.any() or nose.shape != (3,)
            or not np.isfinite(positions).all() or not np.isfinite(nose).all()):
        raise ValueError('三角亮区缺少有效鼻部锚点')
    points = positions[coverage]
    width,height = np.ptp(points[:,0]),np.ptp(points[:,2])
    if min(width,height)<1e-5:
        raise ValueError('三角亮区面部范围退化')
    cheeks = (np.abs(points[:,0]-nose[0])>.1*width)&(np.abs(points[:,0]-nose[0])<.3*width)&(np.abs(points[:,2]-nose[2])<.08*height)
    if not cheeks.any():
        raise ValueError('三角亮区缺少面颊深度参考')
    weights = []
    for side in (-1,1):
        x = side*(positions[...,0]-nose[0])/width
        z = (positions[...,2]-nose[2])/height
        vertices = ((.035,.055),(.18,.025),(.11,-.065))
        distances = []
        for a,b in zip(vertices,vertices[1:]+vertices[:1]):
            dx,dz = b[0]-a[0],b[1]-a[1]
            distances.append((dz*(x-a[0])-dx*(z-a[1]))/math.hypot(dx,dz))
        distances = np.stack(distances)
        nearest = distances.min(axis=0)
        # A smooth minimum rounds the corners without blurring the whole map.
        distance = nearest-.006*np.log(np.exp((nearest-distances)/.006).sum(axis=0))
        weight = np.clip((distance+.0015)/.003,0,1)
        weight = weight*weight*(3-2*weight)
        weight *= coverage&(positions[...,1]<=np.median(points[cheeks,1])+.03*width)
        if (weight>.5).sum()<4:
            raise ValueError('三角亮区的有效纹素不足')
        weights.append(weight.astype(np.float32))
    # shortcut: fixed cheek proportions need visual review; use custom landmarks for unusual faces.
    return *weights,{'status':'generated','anchor':nose.tolist(),'shape':'rounded_triangle'}


THRESHOLD_EDGE_HLSL = r'''
float threshold = LightSide > 0.0 ? SDFValue : 1.0-SDFValue;
float width = max(fwidth(threshold),0.000001)*clamp(EdgePixels,0.0,16.0);
if(threshold <= 0.0) return 1.0;
if(threshold >= 1.0) return 0.0;
if(EdgePixels <= 0.0) return HardShadow;
return 1.0-smoothstep(-0.5*width,0.5*width,threshold-abs(LightSide));
'''


THRESHOLD_ANGLE_HLSL = r'''
float3 L = normalize(LightDirection);
float3 U = normalize(HeadUp);
L -= U*dot(L,U);
float x = dot(L,normalize(HeadForward));
float y = dot(L,normalize(HeadRight));
float yaw = atan2(y,x);
float angle = saturate(abs(yaw)/3.14159265359 + Bias);
if(Contrast > 0.001) {
    float a = tanh(clamp(Contrast,0.0,12.0)*(angle-0.5));
    angle = 0.5+0.5*a/tanh(clamp(Contrast,0.0,12.0)*0.5);
}
return yaw < 0.0 ? -angle : angle;
'''


THRESHOLD_SAMPLE_HLSL = r'''
float2 value = Texture2DSampleLevel(TexObject,TexObjectSampler,TexCoord,0).rg;
// The original negative-offset branch complements this value again.
return LightSide > 0.0 ? value.r : 1.0-value.g;
'''


ATLAS_HLSL = r'''
float3 L = normalize(LightDirection);
float3 F = normalize(HeadForward);
float3 R = normalize(HeadRight);
float3 U = normalize(HeadUp);
float yaw = atan2(dot(L,R), dot(L,F));
float pitch = asin(clamp(dot(L,U),-1.0,1.0));
float x = frac(yaw / 6.28318530718 + 1.0) * 16.0;
// Blender writes bottom-up pixels; FBX import also flips the mesh V axis.
float y = clamp(4.0 - pitch * 8.0 / 3.14159265359,0.0,8.0);
float x0 = floor(x), y0 = min(floor(y),7.0);
float fx = frac(x), fy = y-y0;
float2 uv = float2(UV.x,1.0-UV.y) * UVTransform.xy + UVTransform.zw;
float2 textureUV = float2(uv.x,1.0-uv.y);
float coverage = Texture2DSampleLevel(Coverage,CoverageSampler,textureUV,0).r;
if(coverage < 0.5) return Fallback;
float2 local = (2.5 + saturate(textureUV)*(TileSize-5.0))/TileSize;
float2 a = (float2(x0,y0)+local)/float2(16.0,9.0);
float2 b = (float2(fmod(x0+1.0,16.0),y0)+local)/float2(16.0,9.0);
float2 c = (float2(x0,y0+1.0)+local)/float2(16.0,9.0);
float2 d = (float2(fmod(x0+1.0,16.0),y0+1.0)+local)/float2(16.0,9.0);
float top = lerp(Texture2DSampleLevel(Atlas,AtlasSampler,a,0).r,Texture2DSampleLevel(Atlas,AtlasSampler,b,0).r,fx);
float bottom = lerp(Texture2DSampleLevel(Atlas,AtlasSampler,c,0).r,Texture2DSampleLevel(Atlas,AtlasSampler,d,0).r,fx);
float sdf = lerp(top,bottom,fy);
return smoothstep(-Softness,Softness,sdf);
'''
