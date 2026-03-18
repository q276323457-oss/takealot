# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('takealot_autolister')


a = Analysis(
    ['gui_qt.py'],
    pathex=['/Users/wangfugui/Desktop/重要文件/takealot-autolister/src'],
    binaries=[],
    datas=[('/Users/wangfugui/Desktop/重要文件/takealot-autolister/config', 'config'), ('/Users/wangfugui/Desktop/重要文件/takealot-autolister/input', 'input'), ('/Users/wangfugui/Desktop/重要文件/takealot-autolister/.env.example', '.'), ('/Users/wangfugui/Desktop/重要文件/takealot-autolister/.runtime/build/APP_VERSION.txt', '.'), ('/Users/wangfugui/Desktop/重要文件/takealot-autolister/README.md', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='西安众创南非Takealot自建链接AI工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='西安众创南非Takealot自建链接AI工具',
)
app = BUNDLE(
    coll,
    name='西安众创南非Takealot自建链接AI工具.app',
    icon=None,
    bundle_identifier=None,
)
