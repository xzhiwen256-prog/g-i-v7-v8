"""Explicit outline-color evidence, not game geometry or team ground truth."""
from collections import Counter
import math
from outline_settings import normalize_enemy_color

ENEMY_LABELS=frozenset(('enemy','enemy_head'))


def _box(row,width,height,confidence):
    try:
        x1,y1,x2,y2=map(float,row['box'])
        score=float(row['confidence'])
        if not all(math.isfinite(value) for value in (x1,y1,x2,y2,score)):
            return None
        if score<confidence or x2<=x1 or y2<=y1:
            return None
        if x1<0 or y1<0 or x2>width or y2>height:
            return None
        return x1,y1,x2,y2
    except (KeyError,TypeError,ValueError):
        return None


def _evidence(image,box,head=False,enemy_color='red',friend_guard=False):
    enemy_color=normalize_enemy_color(enemy_color)
    import cv2
    import numpy as np
    x1,y1,x2,y2=box
    width,height=x2-x1,y2-y1
    pad=max(2,min(5,round(width*.06)))
    left,top=max(0,math.floor(x1)-pad),max(0,math.floor(y1)-pad)
    right,bottom=min(image.shape[1],math.ceil(x2)+pad),min(image.shape[0],math.ceil(y2)+pad)
    crop=image[top:bottom,left:right]
    if not crop.size:
        return dict(passed=False,reason='no_pixels')
    hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    hue,saturation,value=hsv[:,:,0],hsv[:,:,1],hsv[:,:,2]
    blue,green,red=(crop[:,:,axis].astype(np.int16) for axis in range(3))
    if enemy_color=='red':
        outline_mask=(((hue<=10)|(hue>=170))&(saturation>=120)&(value>=110)&
                      (red-green>=35)&(red-blue>=25))
    else:
        # Explicit experimental compatibility, not automatic color detection.
        # OpenCV hue units (0..179); thresholds are not fitted to recordings.
        outline_mask=((hue>=125)&(hue<=169)&(saturation>=120)&(value>=110)&
                      (red-green>=25)&(blue-green>=25))
    outline_fraction=float(np.mean(outline_mask))
    friendly=((hue>=35)&(hue<=100)&(saturation>=100)&(value>=90)&(green-red>=25))
    yy,xx=np.ogrid[top:bottom,left:right]
    nx=(xx-x1)/width
    ny=(yy-y1)/height
    # Inspect the head border or upper/body silhouette edges. Red text above a
    # box, a colored object in its center, and the feet do not validate a person.
    if head:
        spatial=((nx<=.28)|(nx>=.72)|(ny<=.3))&(ny>=-.15)&(ny<=1.1)
    else:
        spatial=((nx<=.28)|(nx>=.72)|(ny<=.22))&(ny>=.04)&(ny<=.9)
    spatial=spatial&(nx>=-.15)&(nx<=1.15)
    outline_mask &= spatial
    # Green clothing inside an enemy outline is not a teammate outline.
    # Apply the friendly-color veto only near the outside border.
    friendly &= spatial&((nx<=.12)|(nx>=.88)|(ny<=.08))
    outline_count=int(np.count_nonzero(outline_mask))
    friendly_count=int(np.count_nonzero(friendly))
    outline_rows=int(np.count_nonzero(np.any(outline_mask,axis=1)))
    bands=sum(bool(np.any(outline_mask&((ny>=a)&(ny<b))))
              for a,b in ((.04,.25),(.25,.55),(.55,.9)))
    minimum=max(3,math.ceil((width+height)*.06)) if head else max(10,math.ceil((width+height)*.06))
    needed_rows=max(2,math.ceil(height*.15)) if head else max(5,math.ceil(height*.3))
    friendly_dominates=friendly_count>=max(10,outline_count*3)
    overfilled=outline_fraction>(.65 if head else .35)
    passed=(not friendly_dominates and not overfilled and outline_count>=minimum and outline_rows>=needed_rows and
            (head or bands>=2))
    conflict=False
    guard_metrics={}
    if friend_guard:
        friendly_rows=int(np.count_nonzero(np.any(friendly,axis=1)))
        friendly_min=max(6 if head else 12,math.ceil((width+height)*.04))
        zone_min=max(2,math.ceil(friendly_min/3))
        zones=sum(int(np.count_nonzero(friendly&zone))>=zone_min for zone in (nx<=.12,nx>=.88,ny<=.08))
        conflict=(passed and friendly_count>=friendly_min and friendly_rows>=max(3 if head else 5,math.ceil(height*.12)) and zones>=2)
        guard_metrics=dict(friend_guard=True,friendly_rows=friendly_rows,friendly_zones=zones,mixed_outline_conflict=bool(conflict))
        passed=passed and not conflict
    reason=('mixed_friendly_outline' if conflict else
            'friendly_outline' if friendly_dominates else
            enemy_color+'_fill_ambiguous' if overfilled else
            enemy_color+'_outline_verified' if passed else 'insufficient_'+enemy_color+'_outline')
    if enemy_color=='red':
        # Preserve the legacy red evidence schema exactly.
        return dict(passed=bool(passed),reason=reason,
                    red_pixels=outline_count,friendly_pixels=friendly_count,red_rows=outline_rows,bands=bands,
                    red_fraction=round(outline_fraction,4),**guard_metrics)
    return dict(passed=bool(passed),reason=reason,
                purple_pixels=outline_count,friendly_pixels=friendly_count,purple_rows=outline_rows,bands=bands,
                purple_fraction=round(outline_fraction,4),**guard_metrics)


def _owner(head,body):
    hx1,hy1,hx2,hy2=head
    x1,y1,x2,y2=body
    width,height=x2-x1,y2-y1
    cx,cy=(hx1+hx2)/2,(hy1+hy2)/2
    if not (x1<=cx<=x2 and y1-height*.04<=cy<=y1+height*.4):
        return None
    if hx2-hx1>width*.9 or hy2-hy1>height*.5:
        return None
    intersection=max(0,min(hx2,x2)-max(hx1,x1))*max(0,min(hy2,y2)-max(hy1,y1))
    if intersection/((hx2-hx1)*(hy2-hy1))<.6:
        return None
    return abs(cx-(x1+x2)/2)/width+abs(cy-y1-height*.1)/height


def filter_targets(image,rows,confidence=.3,strict=True,enemy_color='red',friend_guard=False):
    """Annotate every row; only verified linked heads are eligible in strict mode.

    Only screenshot color evidence is available. This cannot prove team identity
    or visibility through map geometry, and deliberately rejects uncertain heads.
    Color is explicit: red preserves the old behavior; purple is experimental.
    Invalid color settings fail closed, even when strict filtering is disabled.
    """
    enemy_color=normalize_enemy_color(enemy_color)
    if type(friend_guard) is not bool:raise ValueError('friend_guard must be a boolean')
    def evidence_for(box,head=False):
        if friend_guard:return _evidence(image,box,head=head,enemy_color=enemy_color,friend_guard=True)
        return _evidence(image,box,head=head,enemy_color=enemy_color)
    output=[dict(row,target_eligible=False,filter_reason='unverified') for row in rows]
    height,width=image.shape[:2]
    boxes={}
    bodies=[]
    for index,row in enumerate(output):
        if (row.get('label') not in ENEMY_LABELS or row.get('team') in ('ally','friendly','teammate') or
                row.get('visible') is False or row.get('occluded') is True):
            row['filter_reason']='class_or_visibility_rejected'
            continue
        box=_box(row,width,height,confidence)
        if box is None:
            row['filter_reason']='invalid_or_low_confidence'
            continue
        boxes[index]=box
        if row['label']=='enemy':
            bodies.append(index)
        if not strict:
            row.update(target_eligible=True,filter_reason='class_only')
    if strict:
        for index in bodies:
            box=boxes[index]
            row=output[index]
            if box[2]-box[0]<8 or box[3]-box[1]<24 or not .12<=(box[2]-box[0])/(box[3]-box[1])<=1.:
                row['filter_reason']='invalid_body_shape'
                continue
            evidence=evidence_for(box)
            row['outline_evidence']=evidence
            row['filter_reason']='body_support_only' if evidence['passed'] else evidence['reason']
        for index,box in boxes.items():
            row=output[index]
            if row['label']!='enemy_head':
                continue
            owners=[(score,body) for body in bodies if (score:=_owner(box,boxes[body])) is not None]
            if not owners:
                row['filter_reason']='head_without_body'
                continue
            owner=min(owners)[1]
            row['linked_body_box']=list(boxes[owner])
            if output[owner]['filter_reason']!='body_support_only':
                row['filter_reason']='body_not_verified'
                continue
            evidence=evidence_for(box,head=True)
            row['outline_evidence']=evidence
            row.update(target_eligible=evidence['passed'],filter_reason=evidence['reason'])
    reasons=Counter(row['filter_reason'] for row in output if not row['target_eligible'])
    report=dict(strict=bool(strict),enemy_color=enemy_color,raw=len(output),
                eligible=sum(row['target_eligible'] for row in output),
                rejected_reasons=dict(reasons),geometry_visibility_verified=False,
                class_counts=dict(Counter(row.get('label','unknown') for row in output)),
                eligible_class_counts=dict(Counter(row.get('label','unknown') for row in output
                                                   if row['target_eligible'])))
    report.update(friend_guard=friend_guard,friend_guard_active=friend_guard and bool(strict))
    return output,report
