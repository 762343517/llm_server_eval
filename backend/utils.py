"""跨模块共用的小工具：数值兜底、展示时区与时间格式化。

这些逻辑原先在多处各写了一份（数值兜底 4 份、展示时区 2 份），口径调整时要同步改多处，
容易走偏，因此收敛到这里。本模块只依赖标准库，不导入项目内其它模块，避免循环依赖。
"""
import os
from datetime import datetime, timedelta, timezone


# ------------------------------------------------------------------ 数值兜底
def int_or(value, default=0):
    """转 int；先经 float 以兼容 "8"、"8.0"、8.9 这类表单/查询串取值，非法值回退 default。

    OverflowError 对应 "inf"/"1e999" 这类能转成 float、却无法转成 int 的取值：收敛前的
    四个副本都只捕 TypeError/ValueError，于是这条路径会把异常抛到接口层（500）。
    按「非法入参回落默认值」的本意在此一并收口。
    """
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def float_or(value, default=0.0, nan_to_default=False):
    """转 float；非法值回退 default，nan_to_default=True 时 NaN 也回退（指标解析用）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if nan_to_default and out != out:      # NaN 是唯一不等于自身的 float
        return default
    return out


def float_or_none(value):
    """转 float；非法值返回 None，用于区分「没填」与「填了 0」。"""
    return float_or(value, None)


# ------------------------------------------------------------------ 展示时区
# 容器/服务器多以 UTC 运行，直接按系统时区渲染会比本地时间少 8 小时。
# 通过 ZHIPU_EVAL_TZ 指定（默认东八区），设为 local/system 则沿用系统时区。
LOCAL_TZ_NAMES = ("", "local", "system", "localtime")
# 镜像缺 tzdata 时，这些名字退回固定 +08:00
TZ_PLUS8_NAMES = ("asia/shanghai", "asia/chongqing", "asia/harbin", "asia/urumqi", "prc")
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def tz_name():
    """展示时区名（未配置时用东八区）。"""
    return (os.environ.get("ZHIPU_EVAL_TZ") or "Asia/Shanghai").strip()


def display_tz(name=None):
    """展示时区的 tzinfo；返回 None 表示沿用系统时区。

    name 缺省取 tz_name()；命名时区加载失败（镜像是精简的、没有 tzdata）时，
    东八区的几个别名退回固定 +08:00，其它名字退回系统时区。
    """
    name = (name if name is not None else tz_name()).strip()
    if name.lower() in LOCAL_TZ_NAMES:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 缺 tzdata 的镜像里东八区退回固定 +08:00
        return timezone(timedelta(hours=8)) if name.lower() in TZ_PLUS8_NAMES else None


def format_ts(ts, fmt=TIME_FMT, tz=None):
    """按展示时区格式化时间戳；ts 为 None 时返回 None。"""
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts) if tz is None else datetime.fromtimestamp(ts, tz)
    return dt.strftime(fmt)
