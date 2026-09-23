"""站点维护窗口核心逻辑。

设计约定：
- 所有起止时间在库中统一存 UTC naive 时间（与全库 `datetime.utcnow` 约定一致）；
  接口入参为 naive 时按站点时区解释，带时区偏移时先换算成 UTC。
- 窗口按半开区间 [start_at, end_at) 生效：恰好等于开始时刻的请求视为窗口内，
  恰好等于结束时刻的请求视为窗口外，所有读路径（站点详情 / 容量日历 /
  换电准入 / 最低容量查询）共用同一套判断，保证边界结果一致。
- 窗口的生效、失效、延期、取消完全由存储字段 + 当前时刻推导，
  不使用任何内存定时器或后台任务，服务重启后行为不变。
- 手工状态优先级最高：站点被手工置为 maintenance/offline 时，
  无论维护窗口如何，站点都不可服务。
"""
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .models import MaintenanceWindow, Station

# 窗口范围
SCOPE_STATION = "station"  # 整站停运
SCOPE_SLOTS = "slots"  # 冻结部分仓位

# 窗口派生状态
STATUS_SCHEDULED = "scheduled"  # 未开始
STATUS_ACTIVE = "active"  # 生效中
STATUS_FINISHED = "finished"  # 已结束
STATUS_CANCELLED = "cancelled"  # 已取消

# 站点有效状态来源：手工状态优先，其次维护窗口
SOURCE_MANUAL = "manual"
SOURCE_WINDOW = "window"
SOURCE_NONE = "none"


def get_station_tz(station: Station) -> ZoneInfo:
    """返回站点时区；配置非法时回退 UTC，保证读路径不炸。"""
    try:
        return ZoneInfo(station.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def validate_timezone(name: str) -> bool:
    """校验 IANA 时区名是否可用。"""
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def to_utc(value: datetime, tz: ZoneInfo) -> datetime:
    """把接口入参时间换算成 UTC naive。

    - naive 时间：按站点时区解释（跨日区间由绝对时刻自然表达，无需特殊处理）；
    - 带偏移的时间：直接换算到 UTC。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(value_utc: datetime, tz: ZoneInfo) -> datetime:
    """把库里的 UTC naive 时间转成站点时区的带偏移时间（用于展示）。"""
    return value_utc.replace(tzinfo=timezone.utc).astimezone(tz)


def window_status(window: MaintenanceWindow, now: datetime) -> str:
    """由存储字段 + 当前时刻推导窗口状态（半开区间）。"""
    if window.cancelled_at is not None:
        return STATUS_CANCELLED
    if now < window.start_at:
        return STATUS_SCHEDULED
    if now < window.end_at:
        return STATUS_ACTIVE
    return STATUS_FINISHED


def is_active_at(window: MaintenanceWindow, t: datetime) -> bool:
    """窗口在时刻 t 是否生效：未取消且 start_at <= t < end_at。"""
    return (
        window.cancelled_at is None
        and window.start_at <= t < window.end_at
    )


def window_contribution(window: MaintenanceWindow, slot_total: int) -> int:
    """单个窗口生效时冻结的仓位数。整站停运冻结全部仓位（跟随站点当前总仓位）。"""
    if window.scope == SCOPE_STATION:
        return slot_total
    return window.frozen_slots or 0


def active_windows_at(windows: Iterable[MaintenanceWindow], t: datetime) -> list[MaintenanceWindow]:
    """筛选在时刻 t 生效的窗口。"""
    return [w for w in windows if is_active_at(w, t)]


def frozen_slots_at(windows: Iterable[MaintenanceWindow], slot_total: int, t: datetime) -> int:
    """时刻 t 被维护窗口冻结的仓位总数（封顶总仓位）。"""
    frozen = sum(window_contribution(w, slot_total) for w in active_windows_at(windows, t))
    return min(frozen, slot_total)


def available_slots_at(station: Station, windows: Iterable[MaintenanceWindow], t: datetime) -> int:
    """时刻 t 的实际可服务仓位数。

    优先级：手工状态（非 running 直接为 0）> 维护窗口冻结。
    """
    if station.status != "running":
        return 0
    return station.slot_total - frozen_slots_at(windows, station.slot_total, t)


def effective_status(
    station: Station, windows: Iterable[MaintenanceWindow], t: datetime
) -> tuple[str, str]:
    """站点在时刻 t 的有效状态及其来源。

    手工状态非 running 时原样返回（来源 manual）；
    否则若存在生效的整站停运窗口则为 maintenance（来源 window）；
    其余情况为 running（来源 none）。部分仓位冻结不改变有效状态，
    只通过 available_slots 体现。
    """
    if station.status != "running":
        return station.status, SOURCE_MANUAL
    if any(w.scope == SCOPE_STATION for w in active_windows_at(windows, t)):
        return "maintenance", SOURCE_WINDOW
    return "running", SOURCE_NONE


def _overlapping_segments(
    windows: Iterable[MaintenanceWindow], start: datetime, end: datetime
) -> list[tuple[datetime, datetime, list[MaintenanceWindow]]]:
    """把 [start, end) 按窗口边界切成若干段，返回 (段起点, 段终点, 覆盖该段的窗口列表)。

    扫描线算法：所有窗口起止点都是切分点，段内覆盖关系不变。
    """
    relevant = [
        w for w in windows if w.start_at < end and w.end_at > start
    ]
    points = {start, end}
    for w in relevant:
        points.add(max(w.start_at, start))
        points.add(min(w.end_at, end))
    ordered = sorted(points)
    segments = []
    for a, b in zip(ordered, ordered[1:]):
        if a >= b:
            continue
        covering = [w for w in relevant if w.start_at <= a and w.end_at >= b]
        segments.append((a, b, covering))
    return segments


def check_capacity_combination(
    existing: Iterable[MaintenanceWindow],
    candidate: MaintenanceWindow,
    slot_total: int,
) -> Optional[str]:
    """校验候选窗口与既有窗口叠加后，任意时刻冻结容量是否超过总仓位。

    只检查候选窗口的生效区间（区间外不受本次变更影响）。
    返回 None 表示合法，否则返回中文错误说明。
    """
    others = [
        w
        for w in existing
        if w.id != candidate.id and w.cancelled_at is None
    ]
    segments = _overlapping_segments(
        list(others) + [candidate], candidate.start_at, candidate.end_at
    )
    for a, b, covering in segments:
        load = sum(window_contribution(w, slot_total) for w in covering)
        if load > slot_total:
            return (
                f"该时段与已有维护窗口叠加后冻结容量达 {load} 仓，"
                f"超过站点总仓位 {slot_total}（冲突时段 {a.isoformat()} ~ {b.isoformat()} UTC）"
            )
    return None


def min_available_between(
    windows: Iterable[MaintenanceWindow],
    slot_total: int,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """[start, end) 内的最低可服务仓位数与最高冻结仓位数（仅考虑维护窗口）。"""
    windows = [w for w in windows if w.cancelled_at is None]
    min_available = slot_total
    max_frozen = 0
    for _, _, covering in _overlapping_segments(windows, start, end):
        frozen = min(
            sum(window_contribution(w, slot_total) for w in covering), slot_total
        )
        max_frozen = max(max_frozen, frozen)
        min_available = min(min_available, slot_total - frozen)
    return min_available, max_frozen


def validate_slot_total_change(
    windows: Iterable[MaintenanceWindow], new_total: int
) -> Optional[str]:
    """调整站点总仓位后，校验全部未取消窗口仍然可行。

    整站停运窗口按新总仓位计负载，因此与任何其他窗口重叠即超载；
    单个冻结窗口的冻结数大于新总仓位同样拒绝。返回 None 合法。
    """
    windows = [w for w in windows if w.cancelled_at is None]
    if not windows:
        return None
    start = min(w.start_at for w in windows)
    end = max(w.end_at for w in windows)
    for a, b, covering in _overlapping_segments(windows, start, end):
        load = sum(
            new_total if w.scope == SCOPE_STATION else (w.frozen_slots or 0)
            for w in covering
        )
        if load > new_total:
            return (
                f"调整后总仓位为 {new_total}，但既有维护窗口在 "
                f"{a.isoformat()} ~ {b.isoformat()} UTC 时段需冻结 {load} 仓，"
                "请先调整或取消相关维护窗口"
            )
    return None


def daily_calendar(
    windows: Iterable[MaintenanceWindow],
    slot_total: int,
    tz: ZoneInfo,
    start_date,
    end_date,
) -> list[dict]:
    """按站点时区的自然日生成容量日历。

    每天返回当日最低可服务仓位、最高冻结仓位，以及当日生效的窗口片段
    （起止时间已按站点时区展示，跨日窗口会出现在覆盖的每一天）。
    """
    windows = [w for w in windows if w.cancelled_at is None]
    days = []
    day = start_date
    while day <= end_date:
        day_start = to_utc(datetime(day.year, day.month, day.day), tz)
        day_end = day_start + timedelta(days=1)
        min_available, max_frozen = min_available_between(windows, slot_total, day_start, day_end)
        day_windows = []
        for w in windows:
            if w.start_at < day_end and w.end_at > day_start:
                day_windows.append(
                    {
                        "id": w.id,
                        "scope": w.scope,
                        "frozen_slots": window_contribution(w, slot_total),
                        "start_local": to_local(w.start_at, tz).isoformat(),
                        "end_local": to_local(w.end_at, tz).isoformat(),
                        "reason": w.reason,
                    }
                )
        days.append(
            {
                "date": day.isoformat(),
                "min_available_slots": min_available,
                "max_frozen_slots": max_frozen,
                "windows": sorted(day_windows, key=lambda item: item["start_local"]),
            }
        )
        day += timedelta(days=1)
    return days
