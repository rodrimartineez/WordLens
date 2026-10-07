# PyInstaller: WordLens como aplicación de ventana (sin consola), en modo carpeta.
#
#   .venv\Scripts\pyinstaller packaging\wordlens.spec --noconfirm --distpath dist --workpath build
#
# Modo carpeta (onedir) en lugar de un único .exe: arranca al instante (onefile
# descomprime todo en %TEMP% en cada inicio) y los antivirus lo marcan mucho
# menos. El instalador de Inno Setup empaqueta esa carpeta.

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH lo define PyInstaller
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "wordlens" / "__init__.py").read_text()).group(1)
nums = tuple(int(n) for n in VERSION.split(".")) + (0,) * (4 - len(VERSION.split(".")))

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=nums, prodvers=nums),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "WordLens"),
                StringStruct("FileDescription", "WordLens"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", "WordLens"),
                StringStruct("OriginalFilename", "WordLens.exe"),
                StringStruct("ProductName", "WordLens"),
                StringStruct("ProductVersion", VERSION),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

datas = [
    (str(ROOT / "assets" / "wordlens.ico"), "assets"),
    (str(ROOT / "assets" / "wordlens.png"), "assets"),
]
# Listas de frecuencias (filtro de palabras básicas): sólo los idiomas que usa
# WordLens. Las de todos los idiomas ocupan ~58 MB.
datas += [
    (src, dst)
    for src, dst in collect_data_files("wordfreq")
    if re.search(r"(large|small)_(en|es)\.msgpack\.gz$", src)
]
datas += collect_data_files("docx")  # plantilla del documento Word nuevo

hiddenimports = ["pystray._win32"]
hiddenimports += collect_submodules("winrt")  # proyecciones WinRT del OCR

a = Analysis(  # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "pygments", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WordLens",
    icon=str(ROOT / "assets" / "wordlens.ico"),
    version=version_info,
    console=False,
    upx=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="WordLens",
)
