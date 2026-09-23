"""换电记录路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import maintenance as mw
from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(r) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, payload.vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    station = db.get(Station, payload.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")

    # 换电准入，优先级从高到低：
    # 1. 手工状态：站点被手工置为维护中/离线时，无论维护窗口如何都不可服务
    if station.status != "running":
        raise HTTPException(status_code=422, detail="换电站当前非运营状态（手工设置），暂停换电服务")
    # 2. 整站停运窗口：生效期间不可服务
    now = datetime.utcnow()
    active_windows = [w for w in station.maintenance_windows if mw.is_active_at(w, now)]
    if any(w.scope == mw.SCOPE_STATION for w in active_windows):
        raise HTTPException(status_code=422, detail="换电站处于整站停运维护窗口，暂停换电服务")
    # 3. 仓位冻结窗口：冻结后无可服务仓位时不可服务
    if station.slot_total - mw.frozen_slots_at(active_windows, station.slot_total, now) <= 0:
        raise HTTPException(status_code=422, detail="维护窗口已冻结全部仓位，暂无可服务仓位")
    if station.battery_ready <= 0:
        raise HTTPException(status_code=422, detail="该换电站暂无满电电池可换")
    if payload.soc_after <= payload.soc_before:
        raise HTTPException(status_code=422, detail="换电后电量应高于换电前电量")

    record = SwapRecord(
        vehicle_id=payload.vehicle_id,
        station_id=payload.station_id,
        soc_before=payload.soc_before,
        soc_after=payload.soc_after,
    )
    # 换电后更新车辆电量、扣减站点可用电池
    vehicle.current_soc = payload.soc_after
    station.battery_ready -= 1
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)
