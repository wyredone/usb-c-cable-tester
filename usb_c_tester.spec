# usb_c_tester.spec
# PyInstaller one-file build spec for USB-C Cable Tester.
# Build:  pyinstaller usb_c_tester.spec
# Output: dist/usb_c_tester.exe

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['usb_c_tester.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'customtkinter',
        'psutil',
        'wmi',
        'win32api',
        'win32com',
        'pythoncom',
        'pywintypes',
        'pystray',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='usb_c_tester',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
