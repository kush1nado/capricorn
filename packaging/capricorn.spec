# PyInstaller: python scripts/build.py  (или pyinstaller packaging/capricorn.spec)
# Результат — dist/Capricorn/: оконный Capricorn(.exe) + консольный capricorn-cli(.exe) с общими библиотеками.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH задаёт PyInstaller

# Textual грузит виджеты и драйверы динамически (web_driver — по строке из TEXTUAL_DRIVER),
# rich — таблицы юникода; PyInstaller сам их не найдёт.
hidden = (collect_submodules("textual") + collect_submodules("rich") + collect_submodules("textual_serve")
          + collect_submodules("capricorn"))
datas = (collect_data_files("textual") + collect_data_files("textual_serve") + collect_data_files("webview")
         + [(str(ROOT / "capricorn" / "web_assets"), "capricorn/web_assets")])   # заставка --web/--desktop


def analysis(script):
    return Analysis(  # noqa: F821
        [str(ROOT / "packaging" / script)],
        pathex=[str(ROOT)],
        hiddenimports=hidden,
        datas=datas,
        excludes=["tkinter", "pytest", "PyInstaller"],
        noarchive=False,
    )


a_gui = analysis("entry_desktop.py")
a_cli = analysis("entry_cli.py")

exe_gui = EXE(  # noqa: F821
    PYZ(a_gui.pure),  # noqa: F821
    a_gui.scripts,
    exclude_binaries=True,
    name="Capricorn",
    console=False,
)
exe_cli = EXE(  # noqa: F821
    PYZ(a_cli.pure),  # noqa: F821
    a_cli.scripts,
    exclude_binaries=True,
    name="capricorn-cli",
    console=True,
)

coll = COLLECT(  # noqa: F821
    exe_gui, a_gui.binaries, a_gui.datas,
    exe_cli, a_cli.binaries, a_cli.datas,
    name="Capricorn",
)

if sys.platform == "darwin":
    app = BUNDLE(coll, name="Capricorn.app", bundle_identifier="dev.kushinado.capricorn")  # noqa: F821
