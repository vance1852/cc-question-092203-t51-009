"""站点维护窗口与容量查询路由（需登录）。

设计要点：
- 窗口生效性完全由时间区间实时推导，无任何内存定时器；取消/延期/重启只改库。
- 时间入参：带偏移的 ISO 时间按其偏移换算；naive 时间按站点时区解释。
- 已开始窗口的每次调整都追加只增审计事件。
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from .. import maintenance as mw
from ..models import MaintenanceWindow, MaintenanceWindowEvent, Station, User
from ..schemas import (
    CapacityDay,
    MaintenanceWindowCancel,
    MaintenanceWindowCreate,
    MaintenanceWindowDetailOut,
    MaintenanceWindowEventOut,
    MaintenanceWindowOut,
    MaintenanceWindowReopen,
    MaintenanceWindowUpdate,
    MinimumCapacityOut,
)

router = APIRouter(tags=["维护窗口"], dependencies=[Depends(get_current_user)])


# ---------- 辅助 ----------
def _load_station(db: Session, station_id: int) -> Station:
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return station


def _load_window(db: Session, window_id: int) -> tuple[MaintenanceWindow, Station]:
    window = db.get(MaintenanceWindow, window_id)
    if not window:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    station = db.get(Station, window.station_id)
    return window, station


def _interpret_instant(value: datetime, tz) -> datetime:
    """入参时间 -> naive UTC：naive 按站点时区解释，aware 按其自身偏移换算。"""
    if value.tzinfo is None:
        return mw.local_naive_to_utc(value, tz)
    return mw.to_utc_naive(value)


def _window_to_out(window: MaintenanceWindow, station: Station, at: datetime | None = None):
    tz = mw.get_station_timezone(station)
    return MaintenanceWindowOut(
        id=window.id,
        station_id=window.station_id,
        title=window.title,
        reason=window.reason,
        start_at=window.start_at.replace(tzinfo=timezone.utc),
        end_at=window.end_at.replace(tzinfo=timezone.utc),
        # 整站停运始终对应当前全部仓位；库内列值仅作创建时快照，供审计事件查看
        freeze_slots=mw.window_freeze(window, station.slot_total),
        whole_station=window.whole_station,
        status=window.status,
        phase=mw.derive_phase(window, at),
        timezone=station.timezone,
        start_at_local=mw.to_local(window.start_at, tz),
        end_at_local=mw.to_local(window.end_at, tz),
        created_by=window.created_by,
        created_at=window.created_at.replace(tzinfo=timezone.utc),
        updated_at=window.updated_at.replace(tzinfo=timezone.utc),
        cancelled_at=window.cancelled_at.replace(tzinfo=timezone.utc) if window.cancelled_at else None,
        cancel_reason=window.cancel_reason,
    )


def _event_to_out(event: MaintenanceWindowEvent) -> MaintenanceWindowEventOut:
    try:
        snapshot = json.loads(event.snapshot)
    except (ValueError, TypeError):
        snapshot = {}
    return MaintenanceWindowEventOut(
        id=event.id,
        action=event.action,
        changed_by=event.changed_by,
        changed_at=event.changed_at,
        note=event.note,
        snapshot=snapshot,
    )


# ---------- 窗口增查 ----------
@router.post(
    "/api/stations/{station_id}/maintenance-windows",
    response_model=MaintenanceWindowOut,
    status_code=status.HTTP_201_CREATED,
)
def create_window(
    station_id: int,
    payload: MaintenanceWindowCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    station = _load_station(db, station_id)
    tz = mw.validate_timezone(station.timezone)

    start_utc = _interpret_instant(payload.start_at, tz)
    end_utc = _interpret_instant(payload.end_at, tz)
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="结束时间必须晚于开始时间（按站点时区解释）")

    if payload.whole_station:
        freeze = station.slot_total
    else:
        if payload.freeze_slots is None:
            raise HTTPException(status_code=422, detail="非整站停运必须指定冻结仓位数 freeze_slots")
        if payload.freeze_slots < 1:
            raise HTTPException(status_code=422, detail="冻结仓位数至少为 1；整站停运请置 whole_station=true")
        freeze = payload.freeze_slots

    # 拒绝重叠后累计冻结容量超过总仓位的组合
    mw.validate_capacity_against_overlap(db, station, start_utc, end_utc, freeze)

    window = MaintenanceWindow(
        station_id=station.id,
        title=payload.title,
        reason=payload.reason,
        start_at=start_utc,
        end_at=end_utc,
        freeze_slots=freeze,
        whole_station=payload.whole_station,
        status=mw.SCHEDULED,
        created_by=current.username,
        updated_at=mw.now_utc(),
    )
    db.add(window)
    db.flush()
    mw.record_event(db, window, "created", changed_by=current.username, note="创建维护窗口")
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station)


@router.get(
    "/api/stations/{station_id}/maintenance-windows",
    response_model=list[MaintenanceWindowOut],
)
def list_windows(
    station_id: int,
    status_filter: str | None = Query(None, alias="status", pattern="^(scheduled|cancelled)$"),
    db: Session = Depends(get_db),
):
    station = _load_station(db, station_id)
    query = db.query(MaintenanceWindow).filter(MaintenanceWindow.station_id == station_id)
    if status_filter:
        query = query.filter(MaintenanceWindow.status == status_filter)
    windows = query.order_by(MaintenanceWindow.start_at.desc(), MaintenanceWindow.id.desc()).all()
    return [_window_to_out(w, station) for w in windows]


@router.get(
    "/api/maintenance-windows/{window_id}",
    response_model=MaintenanceWindowDetailOut,
)
def get_window(window_id: int, db: Session = Depends(get_db)):
    window, station = _load_window(db, window_id)
    out = _window_to_out(window, station).model_dump()
    out["events"] = [_event_to_out(e) for e in window.events]
    return out


# ---------- 窗口调整 ----------
@router.patch("/api/maintenance-windows/{window_id}", response_model=MaintenanceWindowOut)
def update_window(
    window_id: int,
    payload: MaintenanceWindowUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    window, station = _load_window(db, window_id)
    tz = mw.validate_timezone(station.timezone)
    now = mw.now_utc()

    if window.status == mw.CANCELLED:
        raise HTTPException(status_code=409, detail="已取消的维护窗口不可修改")
    phase = mw.derive_phase(window, now)
    if phase == "completed":
        raise HTTPException(status_code=409, detail="窗口已结束；如需延期请使用 reopen")

    data = payload.model_dump(exclude_unset=True)

    # 已开始（active）的窗口：开始时刻不可变，结束时刻只能顺延到未来
    if phase == "active":
        if "start_at" in data:
            new_start = _interpret_instant(data["start_at"], tz)
            if new_start != window.start_at:
                raise HTTPException(status_code=422, detail="窗口已经开始，开始时间不可修改")
        if "end_at" in data:
            new_end = _interpret_instant(data["end_at"], tz)
            if new_end <= now:
                raise HTTPException(status_code=422, detail="窗口进行中，新的结束时间必须晚于当前时间")

    start_utc = _interpret_instant(data["start_at"], tz) if "start_at" in data else window.start_at
    end_utc = _interpret_instant(data["end_at"], tz) if "end_at" in data else window.end_at
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="结束时间必须晚于开始时间（按站点时区解释）")

    whole = data["whole_station"] if "whole_station" in data else window.whole_station
    if whole:
        freeze = station.slot_total
    elif "freeze_slots" in data:
        freeze = data["freeze_slots"]
    elif not window.whole_station:
        freeze = window.freeze_slots  # 原本就是仓位冻结，沿用旧值
    else:
        raise HTTPException(status_code=422, detail="整站停运改为部分冻结时必须显式指定 freeze_slots")
    if not whole and freeze < 1:
        raise HTTPException(status_code=422, detail="冻结仓位数至少为 1；整站停运请置 whole_station=true")

    mw.validate_capacity_against_overlap(
        db, station, start_utc, end_utc, freeze, exclude_window_id=window.id
    )

    old_end = window.end_at
    window.title = data.get("title", window.title)
    window.reason = data.get("reason", window.reason)
    window.start_at = start_utc
    window.end_at = end_utc
    window.whole_station = whole
    window.freeze_slots = freeze
    window.updated_at = now

    action = "extended" if end_utc > old_end else "updated"
    mw.record_event(db, window, action, changed_by=current.username, note="调整维护窗口")
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station)


@router.post("/api/maintenance-windows/{window_id}/cancel", response_model=MaintenanceWindowOut)
def cancel_window(
    window_id: int,
    payload: MaintenanceWindowCancel,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """取消窗口。立即从容量/准入计算中移除，无需任何定时动作。"""
    window, station = _load_window(db, window_id)
    now = mw.now_utc()

    if window.status == mw.CANCELLED:
        raise HTTPException(status_code=409, detail="窗口已取消")
    if mw.derive_phase(window, now) == "completed":
        raise HTTPException(status_code=409, detail="窗口已结束，无需取消")

    window.status = mw.CANCELLED
    window.cancelled_at = now
    window.cancel_reason = payload.reason
    window.updated_at = now
    mw.record_event(
        db, window, "cancelled", changed_by=current.username, note=payload.reason or "取消维护窗口"
    )
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station)


@router.post("/api/maintenance-windows/{window_id}/reopen", response_model=MaintenanceWindowOut)
def reopen_window(
    window_id: int,
    payload: MaintenanceWindowReopen,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """对已结束窗口延期重启（检修超时）：新的结束时间须晚于现在。"""
    window, station = _load_window(db, window_id)
    tz = mw.validate_timezone(station.timezone)
    now = mw.now_utc()

    if window.status == mw.CANCELLED:
        raise HTTPException(status_code=409, detail="已取消的窗口不可重启，请新建窗口")
    if mw.derive_phase(window, now) != "completed":
        raise HTTPException(status_code=409, detail="仅已结束的窗口可以重启；进行中请直接调整结束时间")

    new_end = _interpret_instant(payload.new_end_at, tz)
    if new_end <= now:
        raise HTTPException(status_code=422, detail="新的结束时间必须晚于当前时间")

    mw.validate_capacity_against_overlap(
        db, station, window.start_at, new_end, window.freeze_slots, exclude_window_id=window.id
    )
    window.end_at = new_end
    window.status = mw.SCHEDULED
    window.updated_at = now
    mw.record_event(
        db, window, "reopened", changed_by=current.username, note=payload.reason or "延期重启维护窗口"
    )
    db.commit()
    db.refresh(window)
    return _window_to_out(window, station)


# ---------- 容量查询 ----------
@router.get(
    "/api/stations/{station_id}/capacity/calendar",
    response_model=list[CapacityDay],
)
def capacity_calendar(
    station_id: int,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
):
    """容量日历：按站点本地日期给出每天最低可服务仓位（跨午夜窗口自动落到各天）。"""
    station = _load_station(db, station_id)
    tz = mw.validate_timezone(station.timezone)
    first_day = datetime.strptime(from_date, "%Y-%m-%d").date()
    start_local = datetime.combine(first_day, datetime.min.time())
    start_utc = mw.local_naive_to_utc(start_local, tz)
    end_utc = start_utc + timedelta(days=days)
    return mw.capacity_calendar(db, station, start_utc, end_utc)


@router.get(
    "/api/stations/{station_id}/capacity/minimum",
    response_model=MinimumCapacityOut,
)
def minimum_capacity(
    station_id: int,
    start_at: datetime = Query(...),
    end_at: datetime = Query(...),
    db: Session = Depends(get_db),
):
    """查询未来（或任意）时间段内的最低可服务容量。

    时间参数 naive 时按站点时区解释，带偏移时按偏移换算；半开区间，
    start_at 时刻计入、end_at 时刻不计入。
    """
    station = _load_station(db, station_id)
    tz = mw.validate_timezone(station.timezone)
    start_utc = _interpret_instant(start_at, tz)
    end_utc = _interpret_instant(end_at, tz)
    result = mw.minimum_serviceable_slots(db, station, start_utc, end_utc)
    return MinimumCapacityOut(
        station_id=station.id,
        range_start=start_utc.replace(tzinfo=timezone.utc),
        range_end=end_utc.replace(tzinfo=timezone.utc),
        timezone=station.timezone,
        minimum_serviceable_slots=result["minimum_serviceable_slots"],
        block_reason=result["block_reason"],
        worst_at=result["worst_at"].replace(tzinfo=timezone.utc) if result.get("worst_at") else None,
        window_ids=[w.id for w in result["windows"]],
    )
