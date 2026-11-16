"""DXGI capture with exact monitor matching and an MSS fallback."""
import importlib


def find_output(module, monitor, diagnostics=None):
    """Refresh cached DXcam output geometry, then require one exact match.

    The DXcam factory persists between recognition starts. Its Output.desc may
    still describe the previous desktop/game display mode. DXcam.create itself
    refreshes the chosen descriptor, but our old pre-create match ran too early.
    Never substitute output index 0 or a nearest-size monitor for a failed match.
    """
    factory = getattr(module, '__factory', None)
    found = []
    matches = []
    refreshed = 0
    for device_index, outputs in enumerate(getattr(factory, 'outputs', [])):
        for output_index, output in enumerate(outputs):
            entry = dict(device_index=device_index, output_index=output_index)
            try:
                refresh = getattr(output, 'update_desc', None)
                if callable(refresh):
                    refresh()
                    refreshed += 1
                rect = output.desc.DesktopCoordinates
                bounds = dict(left=rect.left, top=rect.top,
                              width=rect.right-rect.left, height=rect.bottom-rect.top)
                attached = bool(output.attached_to_desktop)
                entry.update(bounds=bounds, attached=attached)
                if attached and bounds == monitor:
                    matches.append((device_index, output_index))
            except Exception as exc:
                # An inaccessible adapter must not force selection of the wrong
                # display. Another freshly matched output can still be usable.
                entry['refresh_error'] = f'{type(exc).__name__}: {exc}'
            found.append(entry)
    if diagnostics is not None:
        diagnostics.update(requested_monitor=dict(monitor), outputs=found,
                           refreshed_outputs=refreshed,
                           policy_revision='capture-output-refresh-20261108')
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError('DXGI 多个输出坐标相同，无法唯一确认所选显示器；未猜测输出编号')
    raise RuntimeError(f'DXGI 刷新后仍未找到与所选显示器坐标一致的输出：需要 {monitor}；输出 {found}')


class ScreenSource:
    def __init__(self,monitor,area,fallback,module_loader=importlib.import_module):
        self.area=dict(area)
        self.fallback=fallback
        self.camera=None
        selection = {}
        self.info=dict(backend='mss',fallback_reason=None,output_match=selection)
        self.region=(area['left']-monitor['left'],area['top']-monitor['top'],
                     area['left']-monitor['left']+area['width'],area['top']-monitor['top']+area['height'])
        try:
            module=module_loader('dxcam')
            device,output=find_output(module,monitor,selection)
            self.camera=module.create(device_idx=device,output_idx=output,output_color='BGR',backend='dxgi')
            self.info=dict(backend='dxgi',fallback_reason=None,device_index=device,output_index=output,output_match=selection)
        except Exception as exc:
            self.info=dict(self.info,backend='mss',fallback_reason=f'{type(exc).__name__}: {exc}')

    def grab(self):
        if self.camera is not None:
            try:
                # Keep owned buffers: previous-frame comparison must not alias
                # DXcam's reusable memory. Cached static frames are OK; the
                # controller already suppresses repeated image corrections.
                image=self.camera.grab(region=self.region,new_frame_only=False)
                if image is not None:
                    if image.shape!=(self.area['height'],self.area['width'],3):
                        raise RuntimeError('DXGI 截图尺寸与所选区域不一致')
                    self.info['backend']='dxgi'
                    self.info['fallback_reason']=None
                    return image,dict(self.area)
            except Exception as exc:
                self.close()
                self.info=dict(self.info,backend='mss',fallback_reason=f'{type(exc).__name__}: {exc}')
        self.info['backend']='mss'
        return self.fallback()

    def close(self):
        if self.camera is not None:
            try:
                self.camera.release()
            except Exception:
                pass
            finally:
                self.camera=None
