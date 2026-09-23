"""维护窗口领域服务。

所有“当前是否停运 / 冻结多少仓位”的判断都在这里依据系统时间与数据库中
的半开区间实时推导，不依赖内存定时器或后台任务：取消、延期、提前恢复
只改数据库，下一次请求立即看到一致结果，服务重启也无影响。

时间约定：
- 库内存 naive datetime，语义为 UTC；
- 窗口为半开区间 ``[start_at, end_at)``：t == start_at 已生效，t == end_at 已恢复；
- 对外（请求/响应）一律使用带时区偏移的 ISO 字符串，按站点时区解释跨日。
"""
import json
from datetime import datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import MaintenanceWindow, MaintenanceWindowEvent, Station

SCHEDULED = "scheduled"
CANCELLED = "cancelled"


# ---------- 时间工具 ----------
def now_utc() -> datetime:
    """当前 UTC 时间（naive，与库内存储一致）。"""
    return datetime.utcnow()


def get_station_timezone(station: Station) -> ZoneInfo:
    try:
        return ZoneInfo(station.timezone)
    except (ZoneInfoNotFoundError, AttributeError):
        return ZoneInfo("Asia/Shanghai")


def validate_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail=f"无法识别的时区：{tz_name}")


def to_utc_naive(dt: datetime) -> datetime:
    """把带时区的时间转为 naive UTC；naive 时间视为 UTC。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def to_local(dt_utc: datetime, tz: ZoneInfo) -> datetime:
    """naive UTC -> 站点本地带时区时间。"""
    return dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)


def local_naive_to_utc(local_dt: datetime, tz: ZoneInfo) -> datetime:
    """把站点本地墙上时间（naive）解释为 UTC naive。"""
    return local_dt.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


# ---------- 查询 ----------
def window_freeze(window: MaintenanceWindow, slot_total: int) -> int:
    """窗口在当前仓位规模下冻结的仓位数。

    whole_station 语义为“整站”，始终冻结当前全部仓位；
    仓位级冻结窗口使用创建时确定的 freeze_slots 快照。
    """
    return slot_total if window.whole_station else window.freeze_slots
def _scheduled_query(db: Session, station_id: int):
    return db.query(MaintenanceWindow).filter(
        MaintenanceWindow.station_id == station_id,
        MaintenanceWindow.status == SCHEDULED,
    )


def active_windows(db: Session, station_id: int, at: Optional[datetime] = None) -> list[MaintenanceWindow]:
    """t 时刻正在生效的窗口：scheduled 且 start_at <= t < end_at。"""
    at = to_utc_naive(at) if at else now_utc()
    return (
        _scheduled_query(db, station_id)
        .filter(MaintenanceWindow.start_at <= at, MaintenanceWindow.end_at > at)
        .order_by(MaintenanceWindow.start_at, MaintenanceWindow.id)
        .all()
    )


def windows_touching_range(
    db: Session, station_id: int, range_start: datetime, range_end: datetime
) -> list[MaintenanceWindow]:
    """与 [range_start, range_end) 有重叠的有效窗口（半开区间相交判定）。"""
    range_start = to_utc_naive(range_start)
    range_end = to_utc_naive(range_end)
    return (
        _scheduled_query(db, station_id)
        .filter(
            MaintenanceWindow.start_at < range_end,
            MaintenanceWindow.end_at > range_start,
        )
        .order_by(MaintenanceWindow.start_at, MaintenanceWindow.id)
        .all()
    )


def derive_phase(window: MaintenanceWindow, at: Optional[datetime] = None) -> str:
    """推导窗口阶段：scheduled 未开始 / active 进行中 / completed 已结束 / cancelled 已取消。"""
    if window.status == CANCELLED:
        return "cancelled"
    at = to_utc_naive(at) if at else now_utc()
    if at < window.start_at:
        return "scheduled"
    if at < window.end_at:
        return "active"
    return "completed"


# ---------- 生效状态与容量 ----------
def frozen_slots(windows: Iterable[MaintenanceWindow], slot_total: int) -> int:
    return sum(window_freeze(w, slot_total) for w in windows)


def effective_state(
    db: Session, station: Station, at: Optional[datetime] = None
) -> dict:
    """某时刻站点的实际可服务状态。

    优先级（高到低）：
    1. 手工 offline —— 设备离线，任何计划都不改变它；
    2. 维护窗口整站停运（whole_station 生效中）；
    3. 手工 maintenance（现场挂牌维护，窗口不能把站点“提前解锁”）；
    4. 仓位级冻结窗口 —— 只削减可服务仓位。
    即：手工状态与窗口取“更不可用”的一方，窗口永不放宽手工下线/维护。
    """
    at = to_utc_naive(at) if at else now_utc()
    windows = active_windows(db, station.id, at)
    whole = next((w for w in windows if w.whole_station), None)
    frozen = min(station.slot_total, frozen_slots(windows, station.slot_total))

    if station.status == "offline":
        effective_status, reason = "offline", "manual_offline"
    elif whole is not None:
        effective_status, reason = "maintenance", "maintenance_window"
    elif station.status == "maintenance":
        effective_status, reason = "maintenance", "manual_maintenance"
    else:
        effective_status, reason = "running", None

    serviceable = effective_status == "running"
    serviceable_slots = 0 if not serviceable else station.slot_total - frozen
    return {
        "at": at,
        "manual_status": station.status,
        "effective_status": effective_status,
        "serviceable": serviceable,
        "block_reason": reason,
        "slot_total": station.slot_total,
        "frozen_slots": frozen,
        "serviceable_slots": serviceable_slots,
        "active_windows": windows,
        "whole_station_window": whole,
    }


def check_swap_admission(db: Session, station: Station, at: Optional[datetime] = None) -> dict:
    """换电准入：返回生效状态；不可换电时抛 422。"""
    state = effective_state(db, station, at)
    if station.status == "offline":
        raise HTTPException(status_code=422, detail="站点已离线，暂不提供换电服务")
    if state["whole_station_window"] is not None:
        raise HTTPException(status_code=422, detail="站点处于维护窗口，整站停运中")
    if station.status == "maintenance":
        raise HTTPException(status_code=422, detail="站点处于维护中，暂不提供换电服务")
    return state


def minimum_serviceable_slots(
    db: Session,
    station: Station,
    range_start: datetime,
    range_end: datetime,
) -> dict:
    """求任意时间段 [range_start, range_end) 内的最低可服务仓位数。

    手工 offline/maintenance 视为覆盖整个查询区间（现场状态在此期间假定不变），
    一旦存在最低可服务容量即为 0。其余由区间内窗口的冻结并集用扫描线取最小值。
    端点与半开区间语义一致：start 时刻在范围内、end 时刻不在。
    """
    range_start = to_utc_naive(range_start)
    range_end = to_utc_naive(range_end)
    if range_end <= range_start:
        raise HTTPException(status_code=422, detail="查询结束时间必须晚于开始时间")

    if station.status != "running":
        return {
            "minimum_serviceable_slots": 0,
            "block_reason": "manual_" + station.status,
            "windows": windows_touching_range(db, station.id, range_start, range_end),
        }

    windows = windows_touching_range(db, station.id, range_start, range_end)
    total = station.slot_total

    # 扫描线：区间内冻结量只在窗口端点处变化；检查起点与每个落在范围内的事件点。
    points = {range_start}
    for w in windows:
        if range_start <= w.start_at < range_end:
            points.add(w.start_at)
        if range_start <= w.end_at < range_end:
            points.add(w.end_at)

    minimum = total
    worst_at = range_start
    for point in sorted(points):
        active = [w for w in windows if w.start_at <= point < w.end_at]
        frozen = min(total, frozen_slots(active, total))
        serviceable = total - frozen
        if serviceable < minimum:
            minimum, worst_at = serviceable, point

    return {
        "minimum_serviceable_slots": minimum,
        "block_reason": "maintenance_window" if minimum == 0 else None,
        "worst_at": worst_at,
        "windows": windows,
    }


# ---------- 容量日历 ----------
def capacity_calendar(
    db: Session, station: Station, day_start: datetime, day_end: datetime
) -> list[dict]:
    """按站点本地日期聚合的容量日历，覆盖 [day_start, day_end) 每一天。

    每天给出当天最低可服务仓位、最大冻结仓位、是否存在整站停运时段。
    跨午夜窗口会同时出现在其覆盖的每一天。
    """
    tz = get_station_timezone(station)
    start = to_utc_naive(day_start)
    end = to_utc_naive(day_end)
    if end <= start:
        raise HTTPException(status_code=422, detail="日历结束日期必须晚于开始日期")

    windows = windows_touching_range(db, station.id, start, end)
    # 本地日期 -> UTC 半开边界
    first_local = to_local(start, tz).date()
    last_local = to_local(end - timedelta(microseconds=1), tz).date()

    days = []
    cur = first_local
    while cur <= last_local:
        day_lo = local_naive_to_utc(datetime.combine(cur, datetime.min.time()), tz)
        day_hi = day_lo + timedelta(days=1)
        lo, hi = max(start, day_lo), min(end, day_hi)
        day_windows = [w for w in windows if w.start_at < hi and w.end_at > lo]

        points = {lo}
        for w in day_windows:
            if lo <= w.start_at < hi:
                points.add(w.start_at)
            if lo <= w.end_at < hi:
                points.add(w.end_at)

        total = station.slot_total
        manual_blocked = station.status != "running"
        min_serviceable = 0 if manual_blocked else total
        max_frozen = 0
        whole_any = False
        for point in sorted(points):
            active = [w for w in day_windows if w.start_at <= point < w.end_at]
            frozen = min(total, frozen_slots(active, total))
            max_frozen = max(max_frozen, frozen)
            if any(w.whole_station for w in active):
                whole_any = True
            serviceable = 0 if manual_blocked else total - frozen
            min_serviceable = min(min_serviceable, serviceable)

        days.append(
            {
                "date": cur.isoformat(),
                "slot_total": total,
                "min_serviceable_slots": min_serviceable,
                "max_frozen_slots": max_frozen,
                "whole_station": whole_any,
                "manual_status": station.status,
                "window_ids": [w.id for w in day_windows],
            }
        )
        cur += timedelta(days=1)
    return days


# ---------- 重叠 / 冻结容量校验 ----------
def validate_all_windows_for_slot_total(
    db: Session, station_id: int, slot_total: int
) -> None:
    """站点仓位总数变更后，所有已排窗口仍须满足：
    单个窗口冻结量 <= 新总仓位，且任意重叠时刻累计冻结量 <= 新总仓位。"""
    windows = (
        db.query(MaintenanceWindow)
        .filter(
            MaintenanceWindow.station_id == station_id,
            MaintenanceWindow.status == SCHEDULED,
        )
        .order_by(MaintenanceWindow.start_at, MaintenanceWindow.id)
        .all()
    )
    points = {w.start_at for w in windows}
    for point in sorted(points):
        total_frozen = 0
        for w in windows:
            if w.start_at <= point < w.end_at:
                fz = window_freeze(w, slot_total)
                if not w.whole_station and fz > slot_total:
                    raise HTTPException(
                        status_code=422,
                        detail=f"已排维护窗口“{w.title}”冻结 {w.freeze_slots} 个仓位，不能把总仓位调减到 {slot_total}",
                    )
                total_frozen += fz
        if total_frozen > slot_total:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"调减到 {slot_total} 个仓位后，已排维护窗口在 {point:%Y-%m-%d %H:%M} UTC "
                    f"累计冻结 {total_frozen}，超过总仓位"
                ),
            )


def validate_capacity_against_overlap(
    db: Session,
    station: Station,
    start_at: datetime,
    end_at: datetime,
    freeze_slots: int,
    *,
    exclude_window_id: Optional[int] = None,
) -> None:
    """拒绝“重叠后冻结容量超过总仓位”的组合。

    把候选区间与所有与之时间相交的已存在窗口放在一起做扫描线：
    任意时刻冻结仓位数之和不得超过站点仓位总数。半开区间下，首尾相接
    （前者 end == 后者 start）不算重叠，互不影响。
    """
    start_at = to_utc_naive(start_at)
    end_at = to_utc_naive(end_at)
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="结束时间必须晚于开始时间")
    if freeze_slots < 0:
        raise HTTPException(status_code=422, detail="冻结仓位数不能为负")
    if freeze_slots > station.slot_total:
        raise HTTPException(status_code=422, detail="冻结仓位数不能超过站点仓位总数")

    others = [
        w
        for w in windows_touching_range(db, station.id, start_at, end_at)
        if w.id != exclude_window_id
    ]

    # 候选在起点处投入；在它的起点以及每个与其重叠窗口的起点检查累计冻结量。
    check_points = {start_at}
    for w in others:
        if start_at <= w.start_at < end_at:
            check_points.add(w.start_at)

    for point in sorted(check_points):
        total_frozen = freeze_slots
        for w in others:
            if w.start_at <= point < w.end_at:
                total_frozen += window_freeze(w, station.slot_total)
        if total_frozen > station.slot_total:
            raise HTTPException(
                status_code=422,
                detail=(
                    "维护窗口重叠导致冻结仓位超过总仓位："
                    f"{point:%Y-%m-%d %H:%M} UTC 时累计冻结 {total_frozen}，总仓位 {station.slot_total}"
                ),
            )


# ---------- 审计痕迹 ----------
def _snapshot(window: MaintenanceWindow) -> str:
    return json.dumps(
        {
            "id": window.id,
            "station_id": window.station_id,
            "title": window.title,
            "reason": window.reason,
            "start_at": window.start_at.isoformat() + "Z",
            "end_at": window.end_at.isoformat() + "Z",
            "freeze_slots": window.freeze_slots,
            "whole_station": window.whole_station,
            "status": window.status,
            "cancelled_at": window.cancelled_at.isoformat() + "Z" if window.cancelled_at else None,
            "cancel_reason": window.cancel_reason,
        },
        ensure_ascii=False,
    )


def record_event(
    db: Session,
    window: MaintenanceWindow,
    action: str,
    *,
    changed_by: str = "",
    note: str = "",
) -> MaintenanceWindowEvent:
    """追加一条只增审计事件。已开始窗口的任何调整都必须经过这里。"""
    event = MaintenanceWindowEvent(
        window_id=window.id,
        action=action,
        changed_by=changed_by,
        note=note,
        snapshot=_snapshot(window),
    )
    db.add(event)
    return event
