"""Native Qt diagnostics on a disposable Windows runner; no network probes or user profile.

The geometry suite requests windows up to 1400 logical pixels high. GitHub's
default remote desktop is smaller than that. Only --configure in GitHub Actions
may change this disposable display; normal developer machines stay untouched.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]


def configure_display():
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':
        raise RuntimeError('--configure is only for the disposable Windows GitHub runner')
    from ctypes import wintypes as wt
    class Mode(ctypes.Structure):
        _fields_=[('device',wt.WCHAR*32),('spec',wt.WORD),('driver',wt.WORD),
                  ('size',wt.WORD),('extra',wt.WORD),('fields',wt.DWORD),
                  ('display',wt.BYTE*16),('color',wt.SHORT),('duplex',wt.SHORT),
                  ('yres',wt.SHORT),('tt',wt.SHORT),('collate',wt.SHORT),
                  ('form',wt.WCHAR*32),('logpixels',wt.WORD),('bits',wt.DWORD),
                  ('width',wt.DWORD),('height',wt.DWORD),('flags',wt.DWORD),
                  ('frequency',wt.DWORD),('icm_method',wt.DWORD),('icm_intent',wt.DWORD),
                  ('media',wt.DWORD),('dither',wt.DWORD),('reserved1',wt.DWORD),
                  ('reserved2',wt.DWORD),('panwidth',wt.DWORD),('panheight',wt.DWORD)]
    assert ctypes.sizeof(Mode)==220
    user=ctypes.WinDLL('user32',use_last_error=True)
    enum=user.EnumDisplaySettingsW;enum.argtypes=[wt.LPCWSTR,wt.DWORD,ctypes.POINTER(Mode)];enum.restype=wt.BOOL
    change=user.ChangeDisplaySettingsW;change.argtypes=[ctypes.POINTER(Mode),wt.DWORD];change.restype=wt.LONG
    def mode_at(index):
        mode=Mode();mode.size=ctypes.sizeof(mode)
        return mode if enum(None,index,ctypes.byref(mode)) else None
    current=mode_at(0xffffffff)
    if current is None:raise ctypes.WinError(ctypes.get_last_error())
    modes=[]
    for i in range(1000):
        mode=mode_at(i)
        if mode is None:break
        if mode.bits>=24 and 900<=mode.width<=3840 and 640<=mode.height<=2160:modes.append(mode)
    modes.sort(key=lambda mode:(mode.height>=1600 and mode.width>=1920,mode.width*mode.height,mode.bits),reverse=True)
    result={'original':[current.width,current.height],
            'available':sorted({(m.width,m.height) for m in modes})}
    for mode in modes:
        mode.fields=0x40000|0x80000|0x100000  # depth, width, height; no registry persistence
        if change(ctypes.byref(mode),2)!=0:continue  # CDS_TEST
        code=change(ctypes.byref(mode),0)
        if code==0:
            after=mode_at(0xffffffff)
            result['configured']=[after.width,after.height]
            print('WINDOWS_CI_DISPLAY '+json.dumps(result),flush=True)
            return
    raise RuntimeError('No usable disposable display mode: '+json.dumps(result))


def qt_diagnostics():
    import desktop
    from qt_support import managed_window,settle_layout
    app=desktop.QApplication.instance() or desktop.QApplication([])
    app.setStyle('Fusion');app.setStyleSheet(desktop.STYLE)
    screen=app.primaryScreen()
    report={'screen':[screen.availableGeometry().width(),screen.availableGeometry().height()],
            'dpr':screen.devicePixelRatio(),'style':app.style().objectName(),'cases':[]}
    def describe(window,name):
        report['cases'].append(dict(case=name,size=[window.width(),window.height()],
            minimum=[window.minimumWidth(),window.minimumHeight()],
            minimum_hint=[window.minimumSizeHint().width(),window.minimumSizeHint().height()],
            port_splitter=window.port_splitter.sizes(),traffic_splitter=window.traffic_splitter.sizes(),
            port_min=window.port_splitter.widget(0).minimumSizeHint().height(),
            monitor_min=window.traffic_splitter.widget(1).minimumSizeHint().height(),
            route_view=window.table.viewport().height(),packet_panel=window.traffic_panel.height()))
    with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'),managed_window(desktop.Window(Path(folder),service_folder=False)) as window:
        window.timer.stop();window.tabs.setCurrentIndex(1);window.show();settle_layout(app)
        describe(window,'stream-first')
        window.resize(1300,window.height());settle_layout(app);describe(window,'stream-wider')
        window.tabs.setCurrentIndex(0);settle_layout(app);describe(window,'network-return')
        for width,height in [(900,640),(1100,720),(1100,1100),(1100,1400)]:
            window.resize(width,height);settle_layout(app);describe(window,f'network-{width}x{height}')
    print('WINDOWS_CI_QT '+json.dumps(report),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--configure',action='store_true');args=parser.parse_args()
    if args.configure:configure_display()
    qt_diagnostics()
