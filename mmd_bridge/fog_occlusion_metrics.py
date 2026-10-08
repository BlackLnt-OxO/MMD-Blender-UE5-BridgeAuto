"""Fog-only image profile comparison; primary geometry never counts as a beam."""
import math
import statistics


def shadow_profile(occluded, unoccluded):
    """Normalized image contrast, rejecting non-finite/zero reference light."""
    if len(occluded)!=len(unoccluded) or len(occluded)<8:
        raise ValueError('雾光测量剖面长度不匹配或太短')
    if any(not math.isfinite(v) or v<0 for v in (*occluded,*unoccluded)):
        raise ValueError('雾光测量包含无效像素')
    return [1-a/max(b,1e-8) for a,b in zip(occluded,unoccluded)]


def band_metrics(reference, candidate):
    """Main central dark band, half-height edges measured in output pixels.

    Outer strips remove the global indirect-light fill pedestal in the A/B
    delta. A weak/absent PT band rejects the ROI instead of reporting success.
    """
    if len(reference)!=len(candidate) or len(reference)<32:
        raise ValueError('雾光测量剖面长度无效')
    if any(not math.isfinite(v) for v in (*reference,*candidate)):
        raise ValueError('雾光测量包含非有限值')
    n=len(reference);outer=max(4,n//8);middle=n//2
    def centered(values):
        pedestal=statistics.median(values[:outer]+values[-outer:])
        return [v-pedestal for v in values]
    reference=centered(reference);candidate=centered(candidate)
    # Central selected occluder, not an unrelated larger shadow at ROI margin.
    left,right=n//3,2*n//3
    peak_index=max(range(left,right),key=lambda i:(reference[i],-abs(i-middle)))
    peak=statistics.mean(reference[max(0,peak_index-1):peak_index+2])
    contrast=statistics.mean(candidate[max(0,peak_index-1):peak_index+2])
    result=dict(reference_contrast=peak,candidate_contrast=contrast,
                reference_valid=peak>=.01,fog_only_roi=True,measurable_change=contrast>=.005)
    def edges(values,threshold,index):
        edges=[]
        for direction in (-1,1):
            i=index
            if values[i]<threshold:return None
            while 0<=i+direction<n and values[i+direction]>=threshold:i+=direction
            j=i+direction
            if not 0<=j<n:return None
            delta=values[j]-values[i]
            edges.append(i+direction*(threshold-values[i])/delta if delta else float(i))
        return edges
    re=edges(reference,peak/2,peak_index)
    ce=edges(candidate,max(contrast,0)/2,peak_index) if contrast>=.005 else None
    result.update(contrast_ratio=max(0,contrast)/peak if peak>0 else 0,
        reference_edges=re,candidate_edges=ce,
        edge_error_px=max(abs(a-b) for a,b in zip(re,ce)) if re and ce else None)
    return result
