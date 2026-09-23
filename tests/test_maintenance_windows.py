"""维护窗口功能测试。

覆盖：整站停运/仓位冻结、站点时区跨日、重叠冻结容量拒绝、半开区间端点
一致性、取消/延期/重启的无定时器边界切换、审计痕迹、手工状态优先级、
容量日历、区间最低可服务容量、换电准入。
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.seed import init_db

init_db()
client = TestClient(app)


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _make_station(headers, slots=10, ready=None, tz="Asia/Shanghai", status="running"):
    resp = client.post(
        "/api/stations",
        json={
            "name": f"窗口测试站{uuid.uuid4().hex[:6]}",
            "slot_total": slots,
            "battery_ready": slots if ready is None else ready,
            "timezone": tz,
            "status": status,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_vehicle(headers):
    resp = client.post(
        "/api/vehicles",
        json={"plate": f"测{uuid.uuid4().hex[:8]}", "current_soc": 10.0},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 创建与整站停运 ----------
def test_whole_station_window_blocks_station_detail_and_swap():
    h = _headers()
    station = _make_station(h, slots=10, ready=10)
    vid = _make_vehicle(h)
    now = datetime.now(timezone.utc)

    resp = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "全站年检",
            "start_at": _utc_iso(now - timedelta(minutes=1)),
            "end_at": _utc_iso(now + timedelta(hours=2)),
            "whole_station": True,
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    w = resp.json()
    assert w["freeze_slots"] == 10
    assert w["phase"] == "active"

    detail = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail["effective_status"] == "maintenance"
    assert detail["serviceable"] is False
    assert detail["serviceable_slots"] == 0
    assert len(detail["active_maintenance_windows"]) == 1

    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vid, "station_id": station["id"], "soc_before": 10.0, "soc_after": 100.0},
        headers=h,
    )
    assert swap.status_code == 422
    assert "维护窗口" in swap.json()["detail"]


def test_partial_freeze_reduces_serviceable_slots_but_allows_swap():
    h = _headers()
    station = _make_station(h, slots=10, ready=10)
    vid = _make_vehicle(h)
    now = datetime.now(timezone.utc)

    resp = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "封 3 个仓位检修",
            "start_at": _utc_iso(now - timedelta(minutes=1)),
            "end_at": _utc_iso(now + timedelta(hours=2)),
            "freeze_slots": 3,
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["whole_station"] is False

    detail = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail["effective_status"] == "running"
    assert detail["serviceable"] is True
    assert detail["frozen_slots"] == 3
    assert detail["serviceable_slots"] == 7

    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": vid, "station_id": station["id"], "soc_before": 10.0, "soc_after": 100.0},
        headers=h,
    )
    assert swap.status_code == 201, swap.text


def test_full_partial_freeze_blocks_swap_without_whole_station():
    h = _headers()
    station = _make_station(h, slots=4, ready=4)
    now = datetime.now(timezone.utc)
    client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "全部仓位冻结",
            "start_at": _utc_iso(now - timedelta(minutes=1)),
            "end_at": _utc_iso(now + timedelta(hours=1)),
            "freeze_slots": 4,
        },
        headers=h,
    )
    detail = client.get(f"/api/stations/{station['id']}", headers=h).json()
    # 非整站停运：状态仍 running，但可服务仓位为 0
    assert detail["effective_status"] == "running"
    assert detail["serviceable_slots"] == 0
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": _make_vehicle(h), "station_id": station["id"], "soc_before": 5.0, "soc_after": 90.0},
        headers=h,
    )
    assert swap.status_code == 422


# ---------- 时区与跨午夜 ----------
def test_naive_local_times_interpreted_by_station_timezone():
    h = _headers()
    # 乌鲁木齐 UTC+6，naive 本地 23:00 应为 UTC 17:00
    station = _make_station(h, slots=8, tz="Asia/Urumqi")
    resp = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "夜间检修",
            "start_at": "2026-10-01T23:00:00",
            "end_at": "2026-10-02T02:00:00",
            "freeze_slots": 2,
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    w = resp.json()
    assert w["start_at"].startswith("2026-10-01T17:00:00")
    assert w["end_at"].startswith("2026-10-01T20:00:00")
    assert w["start_at_local"].startswith("2026-10-01T23:00:00")
    assert w["timezone"] == "Asia/Urumqi"


def test_cross_midnight_window_appears_in_both_days_calendar():
    h = _headers()
    station = _make_station(h, slots=10, tz="Asia/Shanghai")
    resp = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "跨夜检修",
            "start_at": "2026-10-01T23:00:00",
            "end_at": "2026-10-02T02:00:00",
            "whole_station": True,
        },
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    wid = resp.json()["id"]

    cal = client.get(
        f"/api/stations/{station['id']}/capacity/calendar",
        params={"from": "2026-10-01", "days": 2},
        headers=h,
    )
    assert cal.status_code == 200, cal.text
    days = cal.json()
    assert [d["date"] for d in days] == ["2026-10-01", "2026-10-02"]
    for day in days:
        assert day["whole_station"] is True
        assert day["min_serviceable_slots"] == 0
        assert wid in day["window_ids"]

    # 不重叠的 10-03 不受影响
    cal3 = client.get(
        f"/api/stations/{station['id']}/capacity/calendar",
        params={"from": "2026-10-03", "days": 1},
        headers=h,
    ).json()
    assert cal3[0]["min_serviceable_slots"] == 10
    assert cal3[0]["window_ids"] == []


def test_invalid_timezone_rejected_on_station():
    h = _headers()
    resp = client.post(
        "/api/stations",
        json={"name": f"坏时区站{uuid.uuid4().hex[:4]}", "timezone": "Mars/Olympus"},
        headers=h,
    )
    assert resp.status_code == 422


# ---------- 重叠与冻结容量 ----------
def test_overlap_freezing_more_than_total_rejected_but_adjacent_allowed():
    h = _headers()
    station = _make_station(h, slots=10)
    body = {
        "title": "A 封 6",
        "start_at": "2026-11-01T10:00:00",
        "end_at": "2026-11-01T12:00:00",
        "freeze_slots": 6,
    }
    a = client.post(f"/api/stations/{station['id']}/maintenance-windows", json=body, headers=h)
    assert a.status_code == 201, a.text

    # 与 A 重叠且累计 6+5=11 > 10
    overlap = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "B 封 5 重叠",
            "start_at": "2026-11-01T11:00:00",
            "end_at": "2026-11-01T13:00:00",
            "freeze_slots": 5,
        },
        headers=h,
    )
    assert overlap.status_code == 422
    assert "超过总仓位" in overlap.json()["detail"]

    # 半开区间：B' 12:00 开始（== A 结束）不算重叠，允许 5 个
    adjacent = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "B 封 5 首尾相接",
            "start_at": "2026-11-01T12:00:00",
            "end_at": "2026-11-01T13:00:00",
            "freeze_slots": 5,
        },
        headers=h,
    )
    assert adjacent.status_code == 201, adjacent.text

    # 整站停运（封 10）与任何窗口时间重叠都拒绝
    whole = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "想加整站停运",
            "start_at": "2026-11-01T09:00:00",
            "end_at": "2026-11-01T12:30:00",
            "whole_station": True,
        },
        headers=h,
    )
    assert whole.status_code == 422


def test_update_window_must_revalidate_overlap():
    h = _headers()
    station = _make_station(h, slots=10)
    client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "A", "start_at": "2026-12-01T10:00:00", "end_at": "2026-12-01T12:00:00", "freeze_slots": 6},
        headers=h,
    )
    b = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "B", "start_at": "2026-12-01T13:00:00", "end_at": "2026-12-01T14:00:00", "freeze_slots": 5},
        headers=h,
    ).json()
    # 把 B 前移到与 A 重叠应被拒绝
    resp = client.patch(
        f"/api/maintenance-windows/{b['id']}",
        json={"start_at": "2026-12-01T11:00:00", "end_at": "2026-12-01T11:30:00"},
        headers=h,
    )
    assert resp.status_code == 422


# ---------- 半开区间端点一致性 ----------
def test_half_open_boundaries_are_consistent():
    h = _headers()
    station = _make_station(h, slots=5)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    # 窗口恰好此刻开始：t == start 必须立即生效
    starting = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "此刻开始",
            "start_at": _utc_iso(now),
            "end_at": _utc_iso(now + timedelta(hours=1)),
            "whole_station": True,
        },
        headers=h,
    )
    assert starting.status_code == 201, starting.text
    assert starting.json()["phase"] == "active"
    detail = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail["serviceable"] is False

    # 取消后立即恢复（无定时器）
    wid = starting.json()["id"]
    cancelled = client.post(f"/api/maintenance-windows/{wid}/cancel", json={"reason": "提前完工"}, headers=h)
    assert cancelled.status_code == 200
    detail2 = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail2["effective_status"] == "running"
    assert detail2["serviceable"] is True

    # 窗口恰好此刻结束：t == end 必须已恢复（phase=completed）
    ending = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "此刻结束",
            "start_at": _utc_iso(now - timedelta(hours=1)),
            "end_at": _utc_iso(now),
            "freeze_slots": 1,
        },
        headers=h,
    )
    assert ending.status_code == 201
    assert ending.json()["phase"] == "completed"
    detail3 = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail3["frozen_slots"] == 0


# ---------- 审计痕迹 / 已开始窗口约束 ----------
def test_active_window_changes_leave_audit_trail_and_restrict_start():
    h = _headers()
    station = _make_station(h, slots=10)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    w = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "进行中的检修",
            "start_at": _utc_iso(now - timedelta(hours=1)),
            "end_at": _utc_iso(now + timedelta(hours=1)),
            "freeze_slots": 2,
        },
        headers=h,
    ).json()

    # 已开始窗口不允许改开始时间
    bad = client.patch(
        f"/api/maintenance-windows/{w['id']}",
        json={"start_at": _utc_iso(now - timedelta(minutes=30))},
        headers=h,
    )
    assert bad.status_code == 422

    # 延期结束时间：成功并留痕
    ok = client.patch(
        f"/api/maintenance-windows/{w['id']}",
        json={"end_at": _utc_iso(now + timedelta(hours=3)), "title": "进行中的检修-延期"},
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["title"] == "进行中的检修-延期"

    detail = client.get(f"/api/maintenance-windows/{w['id']}", headers=h).json()
    actions = [e["action"] for e in detail["events"]]
    assert actions == ["created", "extended"]
    # 审计快照记录了变更内容，且事件只增
    assert detail["events"][-1]["snapshot"]["title"] == "进行中的检修-延期"

    # 已取消窗口不可再改
    client.post(f"/api/maintenance-windows/{w['id']}/cancel", json={"reason": "完工"}, headers=h)
    again = client.patch(
        f"/api/maintenance-windows/{w['id']}", json={"title": "x"}, headers=h
    )
    assert again.status_code == 409
    detail2 = client.get(f"/api/maintenance-windows/{w['id']}", headers=h).json()
    assert [e["action"] for e in detail2["events"]] == ["created", "extended", "cancelled"]


def test_completed_window_can_reopen_but_not_directly_edit():
    h = _headers()
    station = _make_station(h, slots=10)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    w = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "超时检修",
            "start_at": _utc_iso(now - timedelta(hours=3)),
            "end_at": _utc_iso(now - timedelta(hours=1)),
            "whole_station": True,
        },
        headers=h,
    ).json()
    assert w["phase"] == "completed"

    edit = client.patch(f"/api/maintenance-windows/{w['id']}", json={"title": "新标题"}, headers=h)
    assert edit.status_code == 409

    # 新结束时间不晚于当前时间会被拒绝
    past_end = client.post(
        f"/api/maintenance-windows/{w['id']}/reopen",
        json={"new_end_at": _utc_iso(now - timedelta(minutes=1))},
        headers=h,
    )
    assert past_end.status_code == 422

    # 结束时间在未来：重启成功，窗口重新生效
    ok = client.post(
        f"/api/maintenance-windows/{w['id']}/reopen",
        json={"new_end_at": _utc_iso(now + timedelta(minutes=1))},
        headers=h,
    )
    detail = client.get(f"/api/maintenance-windows/{w['id']}", headers=h).json()
    assert detail["phase"] == "active"
    assert detail["status"] == "scheduled"
    assert [e["action"] for e in detail["events"]] == ["created", "reopened"]
    # 重启后站点重新被冻结
    s = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert s["serviceable"] is False


# ---------- 手工状态与窗口优先级 ----------
def test_manual_status_and_window_priority():
    h = _headers()
    station = _make_station(h, slots=10, ready=10)
    now = datetime.now(timezone.utc)
    # 仓位级冻结（不整站）
    client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "封 2 仓位",
            "start_at": _utc_iso(now - timedelta(minutes=1)),
            "end_at": _utc_iso(now + timedelta(hours=2)),
            "freeze_slots": 2,
        },
        headers=h,
    )

    # 手工切 maintenance：即使窗口只封部分仓位，站点也不可换电
    client.put(f"/api/stations/{station['id']}", json={"status": "maintenance"}, headers=h)
    detail = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail["effective_status"] == "maintenance"
    swap = client.post(
        "/api/swaps",
        json={"vehicle_id": _make_vehicle(h), "station_id": station["id"], "soc_before": 5.0, "soc_after": 90.0},
        headers=h,
    )
    assert swap.status_code == 422

    # 手工恢复 running：窗口仍然冻结 2 个仓位
    client.put(f"/api/stations/{station['id']}", json={"status": "running"}, headers=h)
    detail2 = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail2["effective_status"] == "running"
    assert detail2["serviceable_slots"] == 8

    # 手工 offline 优先级最高：即使有整站停运窗口，理由也是离线
    client.put(f"/api/stations/{station['id']}", json={"status": "offline"}, headers=h)
    detail3 = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail3["effective_status"] == "offline"


# ---------- 区间最低可服务容量 ----------
def test_minimum_serviceable_capacity_query():
    h = _headers()
    station = _make_station(h, slots=10)
    # 2026-09-01 10:00-12:00 本地封 6 个（Shanghai = UTC+8）
    client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "未来封 6",
            "start_at": "2026-09-01T10:00:00",
            "end_at": "2026-09-01T12:00:00",
            "freeze_slots": 6,
        },
        headers=h,
    )

    def query(start, end):
        return client.get(
            f"/api/stations/{station['id']}/capacity/minimum",
            params={"start_at": start, "end_at": end},
            headers=h,
        )

    # 区间覆盖窗口：最低 4
    hit = query("2026-09-01T09:00:00", "2026-09-01T13:00:00")
    assert hit.status_code == 200, hit.text
    assert hit.json()["minimum_serviceable_slots"] == 4
    assert hit.json()["window_ids"]

    # 区间在窗口之前：满容量
    before = query("2026-09-01T06:00:00", "2026-09-01T09:00:00")
    assert before.json()["minimum_serviceable_slots"] == 10

    # 结束时刻恰为窗口开始（半开）：窗口不计入
    edge = query("2026-09-01T09:30:00", "2026-09-01T10:00:00")
    assert edge.json()["minimum_serviceable_slots"] == 10

    # 开始时刻恰为窗口开始：计入
    edge2 = query("2026-09-01T10:00:00", "2026-09-01T10:30:00")
    assert edge2.json()["minimum_serviceable_slots"] == 4

    # 带 UTC 偏移的时间同样正确（10:00 +08:00 == 02:00Z）
    z = query("2026-09-01T02:00:00Z", "2026-09-01T04:00:00Z")
    assert z.json()["minimum_serviceable_slots"] == 4

    # 反向区间拒绝
    bad = query("2026-09-01T12:00:00", "2026-09-01T11:00:00")
    assert bad.status_code == 422


def test_minimum_capacity_with_manual_maintenance_is_zero():
    h = _headers()
    station = _make_station(h, slots=10, status="maintenance")
    resp = client.get(
        f"/api/stations/{station['id']}/capacity/minimum",
        params={"start_at": "2026-09-10T00:00:00", "end_at": "2026-09-11T00:00:00"},
        headers=h,
    )
    assert resp.status_code == 200
    assert resp.json()["minimum_serviceable_slots"] == 0
    assert resp.json()["block_reason"] == "manual_maintenance"


# ---------- 其他校验 ----------
def test_convert_whole_station_to_partial_requires_freeze_slots():
    h = _headers()
    station = _make_station(h, slots=10)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    w = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={
            "title": "整站改部分",
            "start_at": _utc_iso(now - timedelta(hours=1)),
            "end_at": _utc_iso(now + timedelta(hours=2)),
            "whole_station": True,
        },
        headers=h,
    ).json()
    # 未给 freeze_slots 拒绝
    bad = client.patch(
        f"/api/maintenance-windows/{w['id']}", json={"whole_station": False}, headers=h
    )
    assert bad.status_code == 422
    # 显式给 3 个：生效状态回到 running，可服务 7 仓位
    ok = client.patch(
        f"/api/maintenance-windows/{w['id']}",
        json={"whole_station": False, "freeze_slots": 3},
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    detail = client.get(f"/api/stations/{station['id']}", headers=h).json()
    assert detail["effective_status"] == "running"
    assert detail["serviceable_slots"] == 7


def test_window_validation_errors():
    h = _headers()
    station = _make_station(h, slots=10)

    # 结束早于开始
    bad1 = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "反了", "start_at": "2026-08-01T12:00:00", "end_at": "2026-08-01T10:00:00", "freeze_slots": 1},
        headers=h,
    )
    assert bad1.status_code == 422

    # 非整站且未给 freeze_slots
    bad2 = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "没给冻结数", "start_at": "2026-08-01T10:00:00", "end_at": "2026-08-01T12:00:00"},
        headers=h,
    )
    assert bad2.status_code == 422

    # freeze 超过总仓位
    bad3 = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "超量", "start_at": "2026-08-01T10:00:00", "end_at": "2026-08-01T12:00:00", "freeze_slots": 11},
        headers=h,
    )
    assert bad3.status_code == 422

    # 不存在的站点
    bad4 = client.post(
        "/api/stations/999999/maintenance-windows",
        json={"title": "x", "start_at": "2026-08-01T10:00:00", "end_at": "2026-08-01T12:00:00", "freeze_slots": 1},
        headers=h,
    )
    assert bad4.status_code == 404


def test_reducing_station_slot_total_must_respect_scheduled_windows():
    h = _headers()
    station = _make_station(h, slots=10)
    # 已排整站停运：整站窗口始终冻结“全部当前仓位”，调减到 8 仍合法
    client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "未来整站", "start_at": "2027-01-01T10:00:00", "end_at": "2027-01-01T12:00:00", "whole_station": True},
        headers=h,
    )
    ok = client.put(f"/api/stations/{station['id']}", json={"slot_total": 8, "battery_ready": 8}, headers=h)
    assert ok.status_code == 200
    # 整站窗口在新规模下仍冻结全部 8 个仓位
    cal = client.get(
        f"/api/stations/{station['id']}/capacity/calendar",
        params={"from": "2027-01-01", "days": 1},
        headers=h,
    ).json()
    assert cal[0]["slot_total"] == 8
    assert cal[0]["min_serviceable_slots"] == 0
    assert cal[0]["max_frozen_slots"] == 8

    # 另排一个冻结 6 仓位的窗口（不与整站重叠）
    client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "封6", "start_at": "2027-01-02T10:00:00", "end_at": "2027-01-02T12:00:00", "freeze_slots": 6},
        headers=h,
    )
    # 不能把总仓位调到 5（单窗冻结 6 > 5）
    bad = client.put(f"/api/stations/{station['id']}", json={"slot_total": 5, "battery_ready": 5}, headers=h)
    assert bad.status_code == 422

    # 恢复扩容到 12：整站窗口冻结 12，部分窗口仍冻结 6
    up = client.put(f"/api/stations/{station['id']}", json={"slot_total": 12}, headers=h)
    assert up.status_code == 200
    cal2 = client.get(
        f"/api/stations/{station['id']}/capacity/calendar",
        params={"from": "2027-01-02", "days": 1},
        headers=h,
    ).json()
    assert cal2[0]["max_frozen_slots"] == 6
    assert cal2[0]["min_serviceable_slots"] == 6


def test_cancel_completed_window_rejected_and_list_filter():
    h = _headers()
    station = _make_station(h, slots=10)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    past = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "已结束", "start_at": _utc_iso(now - timedelta(hours=2)), "end_at": _utc_iso(now - timedelta(hours=1)), "freeze_slots": 1},
        headers=h,
    ).json()
    assert client.post(f"/api/maintenance-windows/{past['id']}/cancel", json={}, headers=h).status_code == 409

    future = client.post(
        f"/api/stations/{station['id']}/maintenance-windows",
        json={"title": "待执行", "start_at": _utc_iso(now + timedelta(days=1)), "end_at": _utc_iso(now + timedelta(days=2)), "freeze_slots": 1},
        headers=h,
    ).json()
    client.post(f"/api/maintenance-windows/{future['id']}/cancel", json={"reason": "计划变更"}, headers=h)

    cancelled = client.get(
        f"/api/stations/{station['id']}/maintenance-windows", params={"status": "cancelled"}, headers=h
    ).json()
    assert all(w["status"] == "cancelled" for w in cancelled)
    assert any(w["id"] == future["id"] for w in cancelled)
    assert all(w["id"] != past["id"] for w in cancelled)
