"""站点维护窗口路由（需登录）。

维护窗口支持整站停运与部分仓位冻结两种形态；起止时间按站点时区解释，
统一以 UTC 存储；窗口生效/失效由查询时的当前时刻推导，不依赖内存定时器。
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import maintenance as mw
from ..auth import get_current_user
from ..database import get_db
from ..models import MaintenanceWindow, MaintenanceWindowEvent, Station, User
from ..schemas import (
    CapacityCalendarDay,
    MaintenanceWindowCreate,
    MaintenanceWindowEventOut,
    MaintenanceWindowOut,
    MaintenanceWindowUpdate,
    MinCapacityOut,
)

router = APIRouter(prefix="/api/stations", tags=["维护窗口"], dependencies=[Depends(get_current_user)])

# 容量日历单次查询的最大跨度（天）
_CALENDAR_MAX_DAYS = 62


def _get_station_or_404(db: Session, station_id: int) -> Station:
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return station


def _get_window_or_404(db: Session, station_id: int, window_id: int) -> MaintenanceWindow:
    window = db.get(MaintenanceWindow, window_id)
    if not window or window.station_id != station_id:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    return window


def _window_to_out(window: MaintenanceWindow, station: Station, now: datetime) -> MaintenanceWindowOut:
    tz = mw.get_station_tz(station)
    return MaintenanceWindowOut(
        id=window.id,
        station_id=window.station_id,
        scope=window.scope,
        frozen_slots=mw.window_contribution(window, station.slot_total),
        start_at=window.start_at,
        end_at=window.end_at,
        start_local=mw.to_local(window.start_at, tz),
        end_local=mw.to_local(window.end_at, tz),
        status=mw.window_status(window, now),
        reason=window.reason,
        created_by=window.created_by,
        created_at=window.created_at,
        cancelled_at=window.cancelled_at,
    )


def _snapshot(window: MaintenanceWindow, slot_total: int) -> dict:
    """窗口状态快照（写入变更痕迹，时间用 UTC ISO 串）。"""
    return {
        "scope": window.scope,
        "frozen_slots": mw.window_contribution(window, slot_total),
        "start_at": window.start_at.isoformat(),
        "end_at": window.end_at.isoformat(),
        "reason": window.reason,
        "cancelled_at": window.cancelled_at.isoformat() if window.cancelled_at else None,
    }


def _record_event(
    db: Session, window: MaintenanceWindow, action: str, before: Optional[dict], user: User
) -> None:
    db.add(
        MaintenanceWindowEvent(
            window_id=window.id,
            station_id=window.station_id,
            action=action,
            detail={"before": before, "after": _snapshot(window, window.station.slot_total)},
            changed_by=user.username,
        )
    )


def _resolve_scope_and_slots(
    scope: str, frozen_slots: Optional[int], slot_total: int
) -> Optional[int]:
    """校验范围与冻结仓位数的组合，返回应存储的 frozen_slots。"""
    if scope == mw.SCOPE_STATION:
        if frozen_slots is not None:
            raise HTTPException(status_code=422, detail="整站停运无需指定冻结仓位数")
        return None
    if frozen_slots is None:
        raise HTTPException(status_code=422, detail="冻结部分仓位时必须指定冻结仓位数")
    if frozen_slots > slot_total:
        raise HTTPException(status_code=422, detail="冻结仓位数不能超过站点总仓位")
    return frozen_slots


def _parse_range(start_time: datetime, end_time: datetime, station: Station) -> tuple[datetime, datetime]:
    """按站点时区把起止时间换算为 UTC，并校验区间非空（跨日区间自然支持）。"""
    tz = mw.get_station_tz(station)
    start_at = mw.to_utc(start_time, tz)
    end_at = mw.to_utc(end_time, tz)
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="结束时刻必须晚于开始时刻")
    return start_at, end_at


@router.post(
    "/{station_id}/maintenance-windows",
    response_model=MaintenanceWindowOut,
    status_code=status.HTTP_201_CREATED,
)
def create_window(
    station_id: int,
    payload: MaintenanceWindowCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    station = _get_station_or_404(db, station_id)
    start_at, end_at = _parse_range(payload.start_time, payload.end_time, station)
    frozen_slots = _resolve_scope_and_slots(payload.scope, payload.frozen_slots, station.slot_total)

    window = MaintenanceWindow(
        station_id=station.id,
        scope=payload.scope,
        frozen_slots=frozen_slots,
        start_at=start_at,
        end_at=end_at,
        reason=payload.reason,
        created_by=user.username,
    )
    # 拒绝与既有窗口叠加后冻结容量超过总仓位的组合
    conflict = mw.check_capacity_combination(
        station.maintenance_windows, window, station.slot_total
    )
    if conflict:
        raise HTTPException(status_code=422, detail=conflict)

    db.add(window)
    db.flush()  # 先拿到 window.id 再写变更痕迹
    _record_event(db, window, "created", before=None, user=user)
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station, datetime.utcnow())


@router.get("/{station_id}/maintenance-windows", response_model=list[MaintenanceWindowOut])
def list_windows(
    station_id: int,
    status_filter: Optional[str] = Query(
        None, alias="status", pattern="^(scheduled|active|finished|cancelled)$"
    ),
    db: Session = Depends(get_db),
):
    station = _get_station_or_404(db, station_id)
    now = datetime.utcnow()
    windows = (
        db.query(MaintenanceWindow)
        .filter(MaintenanceWindow.station_id == station_id)
        .order_by(MaintenanceWindow.start_at.desc())
        .all()
    )
    out = [_window_to_out(w, station, now) for w in windows]
    if status_filter:
        out = [item for item in out if item.status == status_filter]
    return out


@router.get("/{station_id}/maintenance-windows/{window_id}", response_model=MaintenanceWindowOut)
def get_window(station_id: int, window_id: int, db: Session = Depends(get_db)):
    station = _get_station_or_404(db, station_id)
    window = _get_window_or_404(db, station_id, window_id)
    return _window_to_out(window, station, datetime.utcnow())


@router.put("/{station_id}/maintenance-windows/{window_id}", response_model=MaintenanceWindowOut)
def update_window(
    station_id: int,
    window_id: int,
    payload: MaintenanceWindowUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    station = _get_station_or_404(db, station_id)
    window = _get_window_or_404(db, station_id, window_id)
    now = datetime.utcnow()
    current_status = mw.window_status(window, now)
    if current_status == mw.STATUS_CANCELLED:
        raise HTTPException(status_code=422, detail="已取消的维护窗口不可调整")
    if current_status == mw.STATUS_FINISHED:
        raise HTTPException(status_code=422, detail="已结束的维护窗口不可调整")

    before = _snapshot(window, station.slot_total)
    data = payload.model_dump(exclude_unset=True)
    tz = mw.get_station_tz(station)

    # 合并后的目标状态（未提供的字段沿用现值）
    new_scope = data.get("scope", window.scope)
    new_start = mw.to_utc(data["start_time"], tz) if "start_time" in data else window.start_at
    new_end = mw.to_utc(data["end_time"], tz) if "end_time" in data else window.end_at
    new_reason = data.get("reason", window.reason)
    if "frozen_slots" in data:
        new_frozen = data["frozen_slots"]
    else:
        # 范围未变时沿用现值；范围切换时按新范围重新校验必填性
        new_frozen = window.frozen_slots if new_scope == window.scope else None

    # 已开始的窗口：开始时刻不可再改（保护历史），延期/提前结束只动结束时刻
    if current_status == mw.STATUS_ACTIVE and new_start != window.start_at:
        raise HTTPException(status_code=422, detail="已开始的维护窗口不可修改开始时刻")
    if new_end <= new_start:
        raise HTTPException(status_code=422, detail="结束时刻必须晚于开始时刻")
    new_frozen = _resolve_scope_and_slots(new_scope, new_frozen, station.slot_total)

    # 用合并后的候选状态做容量组合校验（排除自身旧状态）
    candidate = MaintenanceWindow(
        id=window.id,
        station_id=station.id,
        scope=new_scope,
        frozen_slots=new_frozen,
        start_at=new_start,
        end_at=new_end,
    )
    conflict = mw.check_capacity_combination(
        station.maintenance_windows, candidate, station.slot_total
    )
    if conflict:
        raise HTTPException(status_code=422, detail=conflict)

    window.scope = new_scope
    window.frozen_slots = new_frozen
    window.start_at = new_start
    window.end_at = new_end
    window.reason = new_reason
    _record_event(db, window, "updated", before=before, user=user)
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station, now)


@router.post("/{station_id}/maintenance-windows/{window_id}/cancel", response_model=MaintenanceWindowOut)
def cancel_window(
    station_id: int,
    window_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    station = _get_station_or_404(db, station_id)
    window = _get_window_or_404(db, station_id, window_id)
    now = datetime.utcnow()
    current_status = mw.window_status(window, now)
    if current_status == mw.STATUS_CANCELLED:
        raise HTTPException(status_code=422, detail="维护窗口已取消，请勿重复操作")
    if current_status == mw.STATUS_FINISHED:
        raise HTTPException(status_code=422, detail="已结束的维护窗口无需取消")

    before = _snapshot(window, station.slot_total)
    # 取消是持久化状态变更：落库后即生效，不依赖任何定时器
    window.cancelled_at = now
    _record_event(db, window, "cancelled", before=before, user=user)
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station, now)


@router.get(
    "/{station_id}/maintenance-windows/{window_id}/events",
    response_model=list[MaintenanceWindowEventOut],
)
def list_window_events(station_id: int, window_id: int, db: Session = Depends(get_db)):
    _get_station_or_404(db, station_id)
    window = _get_window_or_404(db, station_id, window_id)
    return (
        db.query(MaintenanceWindowEvent)
        .filter(MaintenanceWindowEvent.window_id == window.id)
        .order_by(MaintenanceWindowEvent.id)
        .all()
    )


@router.get("/{station_id}/capacity-calendar", response_model=list[CapacityCalendarDay])
def capacity_calendar(
    station_id: int,
    start_date: Optional[str] = Query(None, description="起始日期（站点时区），缺省为今天"),
    end_date: Optional[str] = Query(None, description="结束日期（站点时区），缺省为起始日后 6 天"),
    db: Session = Depends(get_db),
):
    station = _get_station_or_404(db, station_id)
    tz = mw.get_station_tz(station)
    today = mw.to_local(datetime.utcnow(), tz).date()
    try:
        first = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else today
        last = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else first + timedelta(days=6)
    except ValueError:
        raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD")
    if last < first:
        raise HTTPException(status_code=422, detail="结束日期不能早于起始日期")
    if (last - first).days + 1 > _CALENDAR_MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"单次查询跨度不能超过 {_CALENDAR_MAX_DAYS} 天")

    windows = [w for w in station.maintenance_windows if w.cancelled_at is None]
    return mw.daily_calendar(windows, station.slot_total, tz, first, last)


@router.get("/{station_id}/min-capacity", response_model=MinCapacityOut)
def min_capacity(
    station_id: int,
    start: datetime = Query(..., description="区间开始，naive 时按站点时区解释"),
    end: datetime = Query(..., description="区间结束，naive 时按站点时区解释"),
    db: Session = Depends(get_db),
):
    station = _get_station_or_404(db, station_id)
    start_at, end_at = _parse_range(start, end, station)
    windows = [w for w in station.maintenance_windows if w.cancelled_at is None]
    min_available, max_frozen = mw.min_available_between(
        windows, station.slot_total, start_at, end_at
    )
    tz = mw.get_station_tz(station)
    return MinCapacityOut(
        station_id=station.id,
        start_at=start_at,
        end_at=end_at,
        start_local=mw.to_local(start_at, tz),
        end_local=mw.to_local(end_at, tz),
        min_available_slots=min_available,
        max_frozen_slots=max_frozen,
    )
