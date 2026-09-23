"""换电站管理路由（需登录）。"""
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from .. import maintenance as mw
from ..models import Station, User
from ..schemas import (
    ActiveWindowBrief,
    StationCreate,
    StationOut,
    StationUpdate,
)

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


def _to_out(db: Session, station: Station) -> StationOut:
    """组装站点输出，并附加按当前时间实时推导的维护生效状态。"""
    state = mw.effective_state(db, station)
    return StationOut(
        id=station.id,
        name=station.name,
        address=station.address,
        slot_total=station.slot_total,
        battery_ready=station.battery_ready,
        status=station.status,
        timezone=station.timezone,
        created_at=station.created_at,
        effective_status=state["effective_status"],
        serviceable=state["serviceable"],
        frozen_slots=state["frozen_slots"],
        serviceable_slots=state["serviceable_slots"],
        active_maintenance_windows=[
            ActiveWindowBrief(
                id=w.id,
                title=w.title,
                start_at=w.start_at.replace(tzinfo=timezone.utc),
                end_at=w.end_at.replace(tzinfo=timezone.utc),
                freeze_slots=mw.window_freeze(w, station.slot_total),
                whole_station=w.whole_station,
            )
            for w in state["active_windows"]
        ],
    )


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    return [_to_out(db, s) for s in db.query(Station).order_by(Station.id).all()]


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    if payload.battery_ready > payload.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    mw.validate_timezone(payload.timezone)
    station = Station(**payload.model_dump())
    db.add(station)
    db.commit()
    db.refresh(station)
    return _to_out(db, station)


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return _to_out(db, station)


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    data = payload.model_dump(exclude_unset=True)
    if "timezone" in data:
        mw.validate_timezone(data["timezone"])
    for key, value in data.items():
        setattr(station, key, value)
    if station.battery_ready > station.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    if "slot_total" in data:
        # 已排维护窗口的冻结规模不能因仓位调减而超限
        mw.validate_all_windows_for_slot_total(db, station.id, station.slot_total)
    db.commit()
    db.refresh(station)
    return _to_out(db, station)


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    db.delete(station)
    db.commit()
    return None
