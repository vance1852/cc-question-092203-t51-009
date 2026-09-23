"""换电站管理路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import maintenance as mw
from ..auth import get_current_user
from ..database import get_db
from ..models import MaintenanceWindow, Station, User
from ..schemas import StationCreate, StationOut, StationUpdate

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


def _station_to_out(
    station: Station, windows: list[MaintenanceWindow], now: datetime
) -> StationOut:
    """组装站点响应：基础字段 + 当前生效维护窗口推导出的实时服务能力。"""
    effective, source = mw.effective_status(station, windows, now)
    return StationOut(
        id=station.id,
        name=station.name,
        address=station.address,
        slot_total=station.slot_total,
        battery_ready=station.battery_ready,
        status=station.status,
        timezone=station.timezone,
        created_at=station.created_at,
        effective_status=effective,
        status_source=source,
        frozen_slots=mw.frozen_slots_at(windows, station.slot_total, now),
        available_slots=mw.available_slots_at(station, windows, now),
    )


def _active_windows_by_station(db: Session, now: datetime) -> dict[int, list[MaintenanceWindow]]:
    """取出当前时刻生效的维护窗口并按站点分组（一次查询，避免 N+1）。"""
    rows = (
        db.query(MaintenanceWindow)
        .filter(
            MaintenanceWindow.cancelled_at.is_(None),
            MaintenanceWindow.start_at <= now,
            MaintenanceWindow.end_at > now,
        )
        .all()
    )
    grouped: dict[int, list[MaintenanceWindow]] = {}
    for row in rows:
        grouped.setdefault(row.station_id, []).append(row)
    return grouped


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    grouped = _active_windows_by_station(db, now)
    stations = db.query(Station).order_by(Station.id).all()
    return [_station_to_out(s, grouped.get(s.id, []), now) for s in stations]


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    if payload.battery_ready > payload.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    if not mw.validate_timezone(payload.timezone):
        raise HTTPException(status_code=422, detail="无效的时区名称（应为 IANA 时区，如 Asia/Shanghai）")
    station = Station(**payload.model_dump())
    db.add(station)
    db.commit()
    db.refresh(station)
    return _station_to_out(station, [], datetime.utcnow())


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    now = datetime.utcnow()
    windows = [w for w in station.maintenance_windows if mw.is_active_at(w, now)]
    return _station_to_out(station, windows, now)


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    data = payload.model_dump(exclude_unset=True)
    if "timezone" in data and not mw.validate_timezone(data["timezone"]):
        raise HTTPException(status_code=422, detail="无效的时区名称（应为 IANA 时区，如 Asia/Shanghai）")
    for key, value in data.items():
        setattr(station, key, value)
    if station.battery_ready > station.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    # 缩减总仓位后，既有维护计划叠加冻结量不得超过新总仓位
    if "slot_total" in data:
        plan_conflict = mw.validate_slot_total_change(
            station.maintenance_windows, station.slot_total
        )
        if plan_conflict:
            raise HTTPException(status_code=422, detail=plan_conflict)
    db.commit()
    db.refresh(station)
    now = datetime.utcnow()
    windows = [w for w in station.maintenance_windows if mw.is_active_at(w, now)]
    return _station_to_out(station, windows, now)


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    db.delete(station)
    db.commit()
    return None
