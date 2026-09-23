# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`，含站点时区字段 `timezone`）
- 车辆增删改查（`/api/vehicles`）
- 换电记录查询与登记（`/api/swaps`，会联动更新车辆电量与站点可用电池，并做维护准入）
- 仪表盘统计（`/api/dashboard/stats`）
- 健康检查（`/api/health`）

## 站点维护窗口

设备经理可以安排整站停运或只冻结部分仓位，无需再把整站手工切成“维护中”。

- 窗口按 **半开区间 `[start_at, end_at)`** 处理：恰好开始时刻已生效，恰好结束时刻已恢复；
  是否生效完全由系统时间与库内区间实时推导，**没有任何内存定时器/后台任务**，
  取消、延期、提前恢复、服务重启后的边界切换都只取决于数据库，结果一致。
- 时间入参带时区偏移时按偏移换算；不带偏移（naive）时**按站点时区解释**，因此跨午夜
  区间按现场本地日期处理，响应同时给出 UTC 时间与站点本地时间。
- 重叠窗口会做扫描线校验：**任意时刻累计冻结仓位数不得超过站点仓位总数**，
  否则创建/调整返回 422；首尾相接（前窗结束 == 后窗开始）不算重叠。
- 已开始的窗口开始时间不可改，结束时间只能顺延；每次创建/调整/取消/延期重启都会写入
  **只增审计事件**（`GET /api/maintenance-windows/{id}` 可查变更痕迹与快照）。
- 取消即时生效；已结束但实际超时的窗口可用 `reopen` 延期重启。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/stations/{id}/maintenance-windows` | 创建窗口（`whole_station=true` 或 `freeze_slots=N`） |
| GET | `/api/stations/{id}/maintenance-windows` | 窗口列表（可按 `status` 过滤） |
| GET | `/api/maintenance-windows/{id}` | 窗口详情 + 审计事件 |
| PATCH | `/api/maintenance-windows/{id}` | 调整窗口（已开始者受约束） |
| POST | `/api/maintenance-windows/{id}/cancel` | 取消窗口 |
| POST | `/api/maintenance-windows/{id}/reopen` | 已结束窗口延期重启 |
| GET | `/api/stations/{id}/capacity/calendar?from=YYYY-MM-DD&days=N` | 按站点本地日期的容量日历（跨午夜窗口落到每一天） |
| GET | `/api/stations/{id}/capacity/minimum?start_at=...&end_at=...` | 查询任意时间段内的**最低可服务容量** |

**手工状态与计划的优先级**（取“更不可用”的一方，窗口永不放宽手工下线/维护）：

1. 手工 `offline`（设备离线，最高优先级）；
2. 整站停运维护窗口；
3. 手工 `maintenance`（现场挂牌维护，窗口不能提前解锁）；
4. 仓位级冻结窗口（只削减可服务仓位）。

站点详情中的 `effective_status`、`serviceable`、`frozen_slots`、`serviceable_slots`、
`active_maintenance_windows` 均为按当前时间实时推导的字段；换电准入与容量日历、
最低容量查询使用同一套规则。

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
