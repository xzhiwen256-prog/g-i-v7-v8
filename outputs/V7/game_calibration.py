"""Estimate raw counts/pixel from explicit camera assumptions, not measured data."""
import json
import math


def estimate_gain(sensitivity, width, horizontal_fov_degrees, degrees_per_count_at_sens1):
    values=(sensitivity,width,horizontal_fov_degrees,degrees_per_count_at_sens1)
    if not all(isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
               for value in values):
        raise ValueError('Camera parameters must be finite numbers')
    if sensitivity<=0 or width<=0 or not 0<horizontal_fov_degrees<180 or degrees_per_count_at_sens1<=0:
        raise ValueError('Invalid camera parameters')
    focal_pixels=(width/2)/math.tan(math.radians(horizontal_fov_degrees/2))
    return 1/(focal_pixels*math.radians(degrees_per_count_at_sens1*sensitivity))


def load_calibration(path):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if data['zoomed'] is not False:
            return None
        width,height=data['resolution']
        if not isinstance(height,int) or height<=0:
            return None
        gain=round(estimate_gain(data['sensitivity'],width,data['horizontal_fov_degrees'],
                                 data['degrees_per_count_at_sens1']),2)
        limit=data['step_limit']
        if not .05<=gain<=8 or isinstance(limit,bool) or not isinstance(limit,int) or not 10<=limit<=480:
            return None
        return dict(data,gain=gain,preset_name=f"游戏 {data['sensitivity']:.2f} 估算")
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        return None
