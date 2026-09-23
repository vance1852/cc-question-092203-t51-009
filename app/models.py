"""数据库模型。

业务主题：新能源物流车换电站运营管理。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。"""

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数与当前满电可换电池数
    slot_total = Column(Integer, nullable=False, default=0)
    battery_ready = Column(Integer, nullable=False, default=0)
    # 手工运营状态：running 运营中 / maintenance 维护中 / offline 离线。
    # 注意：维护窗口造成的临时停运不写这个字段，生效状态由时间区间实时推导。
    status = Column(String(16), nullable=False, default="running")
    # 站点时区（IANA 名称，如 Asia/Shanghai），维护窗口的本地时间按它解释
    timezone = Column(String(64), nullable=False, default="Asia/Shanghai")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")
    maintenance_windows = relationship(
        "MaintenanceWindow",
        back_populates="station",
        cascade="all, delete-orphan",
    )


class Vehicle(Base):
    """新能源物流车。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps")
    station = relationship("Station", back_populates="swaps")


class MaintenanceWindow(Base):
    """站点维护窗口。

    时间一律存 naive UTC；对调用方按站点时区展示。窗口是否生效完全由
    ``[start_at, end_at)`` 半开区间实时判断，不依赖任何后台定时任务翻转状态：
    t == start_at 时已生效，t == end_at 时已恢复。

    生命周期 status 只有两种显式状态：
    - scheduled：计划有效（是否正在冻结由当前时间推导为 scheduled/active/completed）
    - cancelled：人工取消（取消后不再参与任何容量计算，且不可复活）
    """

    __tablename__ = "maintenance_windows"

    id = Column(Integer, primary_key=True, index=True)
    station_id = Column(
        Integer, ForeignKey("stations.id"), nullable=False, index=True
    )
    title = Column(String(128), nullable=False)
    reason = Column(String(512), nullable=False, default="")
    # 半开区间 [start_at, end_at)，naive UTC
    start_at = Column(DateTime, nullable=False, index=True)
    end_at = Column(DateTime, nullable=False, index=True)
    # 冻结仓位数（创建时快照）；whole_station=True 时等于当时站点仓位总数
    freeze_slots = Column(Integer, nullable=False, default=0)
    whole_station = Column(Boolean, nullable=False, default=False)
    # scheduled / cancelled
    status = Column(String(16), nullable=False, default="scheduled", index=True)

    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(512), nullable=True)

    station = relationship("Station", back_populates="maintenance_windows")
    events = relationship(
        "MaintenanceWindowEvent",
        back_populates="window",
        cascade="all, delete-orphan",
        order_by="MaintenanceWindowEvent.id",
    )


class MaintenanceWindowEvent(Base):
    """维护窗口变更痕迹（只增不改，审计用）。

    action 取值：created / updated / extended / cancelled / reopened。
    snapshot 为变更发生时窗口完整字段的 JSON 快照。
    """

    __tablename__ = "maintenance_window_events"

    id = Column(Integer, primary_key=True, index=True)
    window_id = Column(
        Integer, ForeignKey("maintenance_windows.id"), nullable=False, index=True
    )
    action = Column(String(24), nullable=False)
    changed_by = Column(String(64), nullable=False, default="")
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    note = Column(String(512), nullable=False, default="")
    snapshot = Column(Text, nullable=False, default="{}")

    window = relationship("MaintenanceWindow", back_populates="events")
