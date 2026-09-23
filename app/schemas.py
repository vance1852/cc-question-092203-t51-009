"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str

    model_config = {"from_attributes": True}


# ---------- 换电站 ----------
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    battery_ready: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")
    # IANA 时区名，维护窗口起止时间按此时区解释
    timezone: str = Field("Asia/Shanghai", max_length=64)


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    battery_ready: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")
    timezone: Optional[str] = Field(None, max_length=64)


class StationOut(StationBase):
    id: int
    created_at: datetime
    # 以下为结合当前生效维护窗口推导的实时服务能力
    # 有效状态：手工状态优先，其次整站停运窗口
    effective_status: str = "running"
    # 有效状态来源：manual 手工 / window 维护窗口 / none 无
    status_source: str = "none"
    # 当前被维护窗口冻结的仓位数（不含手工状态影响）
    frozen_slots: int = 0
    # 当前实际可服务仓位数（手工状态非 running 时为 0）
    available_slots: int = 0

    model_config = {"from_attributes": True}


# ---------- 站点维护窗口 ----------
class MaintenanceWindowCreate(BaseModel):
    """创建维护窗口。起止时间为 naive 时按站点时区解释，支持跨日区间。"""

    scope: str = Field(..., pattern="^(station|slots)$")
    # scope=slots 时必填：冻结的仓位数；scope=station 时忽略
    frozen_slots: Optional[int] = Field(None, ge=1)
    start_time: datetime
    end_time: datetime
    reason: str = Field("", max_length=256)


class MaintenanceWindowUpdate(BaseModel):
    """调整维护窗口。已开始窗口的开始时刻不可再改（保护历史），其余字段可改并留痕。"""

    scope: Optional[str] = Field(None, pattern="^(station|slots)$")
    frozen_slots: Optional[int] = Field(None, ge=1)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    reason: Optional[str] = Field(None, max_length=256)


class MaintenanceWindowOut(BaseModel):
    id: int
    station_id: int
    scope: str
    # 生效时实际冻结的仓位数（整站停运 = 站点总仓位）
    frozen_slots: int
    # UTC 起止（与库内存储一致）
    start_at: datetime
    end_at: datetime
    # 站点时区起止（带偏移，便于调度员阅读）
    start_local: datetime
    end_local: datetime
    # 派生状态：scheduled 未开始 / active 生效中 / finished 已结束 / cancelled 已取消
    status: str
    reason: str
    created_by: str
    created_at: datetime
    cancelled_at: Optional[datetime] = None


class MaintenanceWindowEventOut(BaseModel):
    id: int
    window_id: int
    action: str
    detail: dict
    changed_by: str
    changed_at: datetime


class CapacityCalendarDay(BaseModel):
    date: str
    min_available_slots: int
    max_frozen_slots: int
    windows: list[dict]


class MinCapacityOut(BaseModel):
    station_id: int
    # 查询区间（UTC 与站点时区各一份）
    start_at: datetime
    end_at: datetime
    start_local: datetime
    end_local: datetime
    min_available_slots: int
    max_frozen_slots: int


# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_capacity: float = Field(100.0, gt=0)
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_capacity: Optional[float] = Field(None, gt=0)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(VehicleBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    vehicle_id: int
    station_id: int
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    soc_before: float
    soc_after: float
    swapped_at: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    swap_today: int
    battery_ready_total: int
