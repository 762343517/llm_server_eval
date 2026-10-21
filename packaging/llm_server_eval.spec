# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 FastAPI 后端与 Vue 前端产物打成单文件可执行。

用法：
    python3 -m PyInstaller packaging/llm_server_eval.spec --noconfirm

产物：dist/llm-server-eval（单文件，内置 Python 运行时）
运行：把可执行文件放进发布目录（与 dataset/ model/ runs/ 同级）后 ./llm-server-eval
"""
import os

# PyInstaller 执行 spec 时没有 __file__，用内置的 SPECPATH（spec 所在目录）
ROOT = os.path.dirname(os.path.abspath(SPECPATH))
BACKEND = os.path.join(ROOT, "backend")

# uvicorn / starlette 大量使用运行时动态导入，静态分析抓不到，需显式声明
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 后端自身模块（api.py 通过 sys.path 同目录导入）
    "agent_core",
    "appconfig",
    "consts",
    "csvview",
    "dataset_registry",
    "eval_core",
    "perf_core",
    "perf_manager",
    "run_manager",
    "runtime_paths",
    "task_queue",
    "utils",
]

a = Analysis(
    [os.path.join(BACKEND, "api.py")],
    pathex=[BACKEND],
    binaries=[],
    # 前端构建产物随包内置；运行期解包到 <_MEIPASS>/webui
    datas=[(os.path.join(BACKEND, "webui"), "webui")],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除与评测服务无关的大件，控制体积
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "pytest",
        "notebook",
        "PyQt5",
        "PySide2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="llm-server-eval",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
