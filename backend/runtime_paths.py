"""运行期路径基准：区分「只读资源」与「可写数据根」。

开发态：直接运行 `python3 backend/api.py`，资源与数据都在项目根目录。
打包态：PyInstaller 单文件运行时，只读资源（前端产物）位于解包目录
`sys._MEIPASS`，而数据目录（dataset/ model/ runs/）必须落在可执行文件旁边，
否则重启后评测结果与配置会随临时目录一起消失。

可用环境变量 APP_ROOT 显式指定数据根（适合把可执行文件与数据分开部署）。
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))


def bundle_dir():
    """只读资源根：打包后为 PyInstaller 解包目录，开发态为项目根。"""
    if FROZEN:
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return os.path.abspath(meipass)
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_root():
    """可写数据根：dataset/ model/ runs/ 都相对它定位。"""
    override = (os.environ.get("APP_ROOT") or "").strip()
    if override:
        return os.path.abspath(override)
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return bundle_dir()


def webui_dir():
    """前端构建产物目录：打包时被放到 <bundle>/webui，开发态在 backend/webui。"""
    if FROZEN:
        return os.path.join(bundle_dir(), "webui")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
