"""Numeric input validation and view binding. No Tk, device or config I/O."""
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import math
import unicodedata


@dataclass(frozen=True)
class NumberSpec:
    minimum:float
    maximum:float
    decimals:int
    step:float

    @property
    def range_hint(self):return f'{self.minimum:g}–{self.maximum:g}'


SPECS={
    'gain_slider':NumberSpec(.05,8,2,.01),
    'limit_slider':NumberSpec(10,480,0,1),
    'response_slider':NumberSpec(6,250,0,1),
    'chase_slider':NumberSpec(1,6,1,.1),
    'tolerance_slider':NumberSpec(0,20,1,.1),
    'fire_release_slider':NumberSpec(0,1000,0,1),
    'pd_kp_slider':NumberSpec(.25,2,2,.01),
    'pd_kd_slider':NumberSpec(0,1,2,.01),
    'confidence_slider':NumberSpec(.1,.95,2,.01),
    'aim_slider':NumberSpec(5,50,0,1),
}


def parse_value(text,spec):
    hint=('请输入整数' if spec.decimals==0 else '请输入数值')+f'（{spec.range_hint}）'
    try:
        value=Decimal(unicodedata.normalize('NFKC',str(text)).strip())
        if not value.is_finite() or not Decimal(str(spec.minimum))<=value<=Decimal(str(spec.maximum)):
            raise ValueError(hint)
        if spec.decimals==0 and value!=value.to_integral_value():raise ValueError(hint)
        value=value.quantize(Decimal(1).scaleb(-spec.decimals),rounding=ROUND_HALF_UP)
        return int(value) if spec.decimals==0 else float(value)
    except (InvalidOperation,TypeError):raise ValueError(hint) from None


def format_value(value,spec):
    if not math.isfinite(float(value)):raise ValueError('Invalid slider value')
    return f'{float(value):.{spec.decimals}f}'


class NumericBinding:
    def __init__(self,spec,read_text,write_text,read_slider,write_slider,callback,show_error,set_state):
        self.spec=spec;self.read_text=read_text;self.write_text=write_text
        self.read_slider=read_slider;self.write_slider=write_slider
        self.callback=callback;self.show_error=show_error;self.set_state=set_state
        self.setting=False;self.disabled=False

    def sync(self,*args):
        self.write_text(format_value(self.read_slider(),self.spec));self.show_error(None)

    def slider_changed(self,value):
        if self.setting:return
        self.callback(value)
        self.sync()

    def commit(self,event=None):
        if self.disabled:self.sync();return 'break'
        raw=self.read_text()
        try:value=parse_value(raw,self.spec)
        except ValueError as exc:self.show_error(str(exc));return 'break'
        old=float(self.read_slider())
        if not math.isclose(float(value),old,rel_tol=0.,abs_tol=1e-12):
            self.setting=True
            try:self.write_slider(value)
            finally:self.setting=False
            try:self.callback(str(value))
            except (ValueError,TypeError) as exc:
                self.setting=True
                try:self.write_slider(old)
                finally:self.setting=False
                self.write_text(raw);self.show_error('未应用：'+str(exc));return 'break'
        self.sync();return 'break'

    def revert(self,event=None):self.sync();return 'break'

    def set_disabled(self,disabled):
        self.disabled=bool(disabled)
        self.set_state('disabled' if self.disabled else 'normal')
        if self.disabled:self.sync()
