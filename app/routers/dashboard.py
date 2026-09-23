"""仪表盘统计路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import maintenance as mw
from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    stations = db.query(Station).all()

    # 运营站数按"有效状态"统计：手工状态优先，其次整站停运维护窗口
    effective_running = 0
    for station in stations:
        effective, _ = mw.effective_status(
            station,
            [
                w
                for w in station.maintenance_windows
                if w.cancelled_at is None and w.start_at <= now < w.end_at
            ],
            now,
        )
        if effective == "running":
            effective_running += 1

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return DashboardStats(
        station_total=len(stations),
        station_running=effective_running,
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord).filter(SwapRecord.swapped_at >= today_start).count(),
        battery_ready_total=sum(station.battery_ready for station in stations),
    )
