"""换电记录路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from .. import maintenance as mw
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
    # 维护窗口 / 手工维护 / 离线准入：纯时间推导，无定时器依赖
    state = mw.check_swap_admission(db, station)
    # 仓位级冻结窗口会压缩可服务仓位，可用满电电池不能超过可服务仓位数
    available_ready = min(station.battery_ready, state["serviceable_slots"])
    if available_ready <= 0:
        raise HTTPException(status_code=422, detail="该换电站当前冻结仓位过多，暂无可换满电电池")
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
