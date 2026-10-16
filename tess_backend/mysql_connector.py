"""P5 · 直连网盟 MySQL 取数连接器（MysqlDataConnector）。

替代 TeensingDataConnector 经 HTTP 调 /report 等接口，直接查网盟 MySQL
（即 tess_ai_pb 已连接的 172.22.0.6 那台）的预聚合报表表 + 实体表。

设计要点：
- 实现与 TeensingDataConnector 同一套 api_get(path, params, token, timeout) 接口，
  返回 dict 形状与 Teensing HTTP 接口**完全一致**，因此 18 种 analysis_type 取数逻辑
  一行不动，仅底层数据源切换（见 data_connector.get_data_connector 的 mysql 分支）。
- 平台级隔离：按 platform_id 选库（Melodong→melocpi / seanear→seanear）。
- 运营级隔离：operator_id 给定时，advertiser 维度查询注入 WHERE am = operator_id。
- profit = revenue - payout（已确认无调整项）。

取数真相（实测核对，2026-10-15）：
- 唯一带 campaign_id 的明细表是 report_hour_new（小时级；含 revenue/payout/clicks/
  conversions + campaign_id/advertiser_id/date/hour）。report_day 仅有
  advertiser/publisher/country 维度、无 campaign_id；report_campaign 整表为空。
- **单 campaign 维度 /report 的权威数据源 = report_hour_new**（与线上 HTTP 接口逐字段
  完全一致：单 campaign 窗口营收 8298.0 两边吻合）。
- report_month 是「按月的预聚合」，对**已结月**与 report_hour_new 逐月完全吻合
  （123.9=123.9 … 3181=3181），但**不含当前进行月**（月度汇总通常在月末才生成，
  故 2026-10 缺），且偶发个别历史月缺口，且无 clicks 列。

性能与策略（关键）：
- report_hour_new 全广告主聚合（GROUP BY campaign_id WHERE advertiser_id=…）在大广告主
  上约 60s+，超过 analytics 45s 取数预算 → 不可用于「全量聚合」。
- 但 report_hour_new 对**小 IN 列表**（≤数百 campaign_id）走 campaign_id 索引极快
  （30 个 campaign 4 个月明细仅 0.15s）。
- 故采用混合策略：
  * 排名/汇总先走 report_month（0.4s，覆盖已结月）拿到候选 campaign 与已结月营收；
  * 对排名靠前的 top-N（默认 500，覆盖实际消费的 top 30）用 report_hour_new 小 IN
    回填**完整窗口营收/payout/clicks/conversions**（含缺口月 + 当前进行月）→ 与线上
    接口逐字段吻合，且查询仍极快。
  * 趋势（campaign_trends_monthly）直接读 report_hour_new 日级明细（scoped 30 个，0.15s），
    交由上层 _monthly_from_daily 聚合，与线上路径完全一致。
- 切换开关：TESS_DATA_CONNECTOR=mysql 启用；默认 teensing（HTTP 接口），零风险回退。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("tess_backend.mysql_connector")

# campaign.status 数字 → 可读名（已与真实接口对齐：1=Active / 2=Paused / 3=Stop）
CAMPAIGN_STATUS_LABELS = {1: "Active", 2: "Paused", 3: "Stop"}

# 平台 → 业务库 默认路由（与 platform 选库一致；可被 TESS_MYSQL_DATABASES 覆盖）
DEFAULT_PLATFORM_DB = {"Melodong": "melocpi", "seanear": "seanear"}


def campaign_status_label(status) -> Optional[str]:
    """把 campaign.status 数字映射为人类可读名；无法识别返回 None。"""
    if status is None:
        return None
    try:
        return CAMPAIGN_STATUS_LABELS.get(int(status))
    except (TypeError, ValueError):
        return None


class MysqlDataConnector:
    """生产用：直连网盟 MySQL，按路径模拟 Teensing HTTP 报表接口返回。

    必需环境变量（生产放 Tess .env，与 tess_ai_pb 同源）：
      TESS_MYSQL_HOST（默认 172.22.0.6）、TESS_MYSQL_PORT（默认 3306）、
      TESS_MYSQL_USER（默认 melo）、TESS_MYSQL_PASSWORD、
      TESS_MYSQL_DATABASES（JSON：{"Melodong":"melocpi","seanear":"seanear"}，可选）
    """

    def __init__(
        self,
        platform_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        databases: Optional[dict] = None,
        timeout: int = 15,
        fill_top_n: int = 500,
    ):
        self.platform_id = platform_id or "Melodong"
        self.operator_id = operator_id
        self.host = host or os.getenv("TESS_MYSQL_HOST", "172.22.0.6")
        self.port = int(port or os.getenv("TESS_MYSQL_PORT", "3306"))
        self.user = user or os.getenv("TESS_MYSQL_USER", "melo")
        self.password = password if password is not None else os.getenv("TESS_MYSQL_PASSWORD", "")
        raw_db = databases or os.getenv("TESS_MYSQL_DATABASES")
        if isinstance(raw_db, str) and raw_db:
            try:
                self.databases = json.loads(raw_db)
            except (ValueError, TypeError):
                self.databases = dict(DEFAULT_PLATFORM_DB)
        elif isinstance(raw_db, dict):
            self.databases = raw_db
        else:
            self.databases = dict(DEFAULT_PLATFORM_DB)
        self.timeout = timeout
        self.database = self.databases.get(
            self.platform_id, self.databases.get("Melodong", "melocpi")
        )
        # 用 report_hour_new 回填「完整窗口」的 top-N campaign 数量（覆盖实际消费的 top 30）。
        # 越大越准（排名越不易因缺口月/当前月错位），但 IN 列表越长查询越慢；500 在实测中 <1s。
        self.fill_top_n = int(fill_top_n or 500)
        # 重聚合查询结果进程内缓存，避免长周期下钻对同一 (campaign_ids,ds,de) 重复跑分页查询。
        self._agg_cache: dict = {}
        # 跨库回退：默认关（=0，生产默认）。仅当默认库（按 platform_id 选）查不到时，
        # 才允许自动尝试 TESS_MYSQL_DATABASES 中其它库。开启会导致平台串档
        # （如 seanear 的榜单借到 Melodong 的 campaign/利润）——运营隔离由 platform_id 决定，
        # 不应被回退掩盖。需要时用 TESS_MYSQL_CROSS_DB=1 临时开启。
        self.cross_db = str(os.getenv("TESS_MYSQL_CROSS_DB", "0") or "0").strip() not in (
            "0", "false", "no", "off"
        )
        # 路由审计：记录每个被调用的 path 由 MySQL 实现还是回退到真实 API（供对拍/可观测用）。
        self.served_routes: list = []
        # 试点期回退实例（惰性创建，避免与 data_connector 循环 import）。
        self._api_fallback = None

    # ----- 传输层（替代 Teensing 的 HTTP） -----

    def _connect(self):
        try:
            import pymysql
        except ImportError as e:  # 仅 mysql 模式才需要，未装时给明确指引
            raise RuntimeError(
                "MysqlDataConnector 需要 pymysql（pip install pymysql）；"
                "非 mysql 模式不应触发此错误"
            ) from e
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            connect_timeout=self.timeout,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _query(self, sql: str, params: tuple = ()) -> list:
        """执行查询，返回 list[dict]（DictCursor）。库 = self.database。"""
        return self._query_db(self.database, sql, params)

    def _query_db(self, db: str, sql: str, params: tuple = ()) -> list:
        """连到指定库执行查询（跨库探测用，如 self_check / 跨库回退）。"""
        import pymysql

        conn = pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=db,
            charset="utf8mb4",
            connect_timeout=self.timeout,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    def _query_across(self, sql: str, params: tuple = ()) -> list:
        """先查默认库（self.database）；试点期若空且开了跨库回退，依次试其它库。

        返回首个非空结果；全空则回退默认库空结果（与上层「无数据」语义一致）。
        仅报表类查询走这里（campaign/advertiser 实体查询保持平台库内，不跨库）。
        """
        rows = self._query(sql, params)
        if not rows and self.cross_db:
            for db in self.databases.values():
                if db == self.database:
                    continue
                try:
                    r2 = self._query_db(db, sql, params)
                except Exception as e:  # 其它库连不上/无表，跳过继续试
                    logger.warning("MysqlDataConnector 跨库探测 %s 失败，跳过：%s", db, e)
                    continue
                if r2:
                    logger.warning(
                        "MysqlDataConnector 跨库回退：默认库 %s 无数据，改从 %s 取（%d 行）",
                        self.database, db, len(r2),
                    )
                    return r2
        return rows

    # ----- 日期工具 -----

    @staticmethod
    def _ym_range(ds: str, de: str):
        """返回 [ds, de] 区间覆盖的所有自然月，形如 [202607, 202608, ...]（int YYYYMM）。"""
        d0 = datetime.strptime(ds, "%Y-%m-%d")
        d1 = datetime.strptime(de, "%Y-%m-%d")
        out, y, m = [], d0.year, d0.month
        while (y, m) <= (d1.year, d1.month):
            out.append(y * 100 + m)
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return out

    @staticmethod
    def _current_ym() -> int:
        """当前进行月 YYYYMM（report_month 通常不含此月——月度汇总月末才生成）。"""
        n = datetime.now()
        return n.year * 100 + n.month

    @staticmethod
    def _f(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    def self_check(
        self, advertiser_id: int = 1000853,
        ds: str = "2026-07-01", de: str = "2026-10-31",
    ) -> dict:
        """只读自检：确认连通性 + 每个库里该广告主的数据分布。

        返回 dict 供 verify_mysql.py 打印；生产排障一眼看出「连没连上 / 数据在哪个库」。
        不抛异常。
        """
        out: dict = {
            "host": self.host, "port": self.port, "user": self.user,
            "default_db": self.database, "databases": self.databases, "cross_db": self.cross_db,
            "dbs": {},
        }
        for name, db in self.databases.items():
            entry = {"db": db, "reachable": False, "error": None,
                     "report_month_rows": None, "report_hour_new_rows": None}
            try:
                rm = self._query_db(
                    db, "SELECT COUNT(*) c FROM report_month WHERE advertiser_id=%s", (advertiser_id,))
                entry["report_month_rows"] = rm[0]["c"] if rm else None
                rh = self._query_db(
                    db, "SELECT COUNT(*) c FROM report_hour_new WHERE advertiser_id=%s AND date BETWEEN %s AND %s",
                    (advertiser_id, ds, de))
                entry["report_hour_new_rows"] = rh[0]["c"] if rh else None
                entry["reachable"] = True
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {e}"
            out["dbs"][name] = entry
        return out

    # ----- 路由：模拟 Teensing HTTP 接口返回形状 -----

    def api_get(
        self,
        path: str,
        params: Optional[dict] = None,
        token: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> dict:
        """公开透传：与 TeensingDataConnector.api_get 同签名，返回已「解包」的业务 dict。

        已实现的路径（report/campaigns/advertiser/daily-kpi/kpi-trend）由 MySQL 直出；
        未实现的路径**回退到真实 Teensing API**（试点期零风险，绝不静默返回空导致数据丢失）。
        每次调用都记一笔 served_routes，便于对拍时区分「MySQL 校验」与「API 委托」。
        """
        params = params or {}
        if path == "/advertisers/campaign-daily-kpi":
            self.served_routes.append((path, "mysql"))
            return self._campaign_daily_kpi(params)
        if path.startswith("/advertisers/"):
            self.served_routes.append((path, "mysql"))
            aid = path.split("/advertisers/", 1)[1]
            return self._advertiser_profile(aid)
        if path == "/report":
            self.served_routes.append((path, "mysql"))
            return self._report(params)
        if path == "/campaigns":
            self.served_routes.append((path, "mysql"))
            return self._campaigns(params)
        if path == "/campaign-kpi-trend":
            self.served_routes.append((path, "mysql"))
            return self._campaign_kpi_trend(params)
        if path == "/publishers":
            self.served_routes.append((path, "mysql"))
            return self._publishers(params)
        if path == "/campaign-quality/publisher":
            self.served_routes.append((path, "mysql"))
            return self._publisher_quality(params)
        if path == "/campaign-quality":
            self.served_routes.append((path, "mysql"))
            return self._campaign_quality(params)
        if path == "/campaign-ctit-etit":
            self.served_routes.append((path, "mysql"))
            return self._ctit_etit(params)
        # 未实现：回退到真实 Teensing API（绝不静默返回空，避免数据丢失）。
        self.served_routes.append((path, "api"))
        if self._api_fallback is None:
            from tess_backend.data_connector import TeensingDataConnector, get_platform_base_url, _db_data_token
            self._api_fallback = TeensingDataConnector(
                base_url=get_platform_base_url(self.platform_id))
        return self._api_fallback.api_get(path, params, token, timeout)

    # ----- 实体 / 报表实现 -----

    def _advertiser_profile(self, aid: str) -> dict:
        """模拟 GET /advertisers/{aid}：广告主档案。"""
        am_clause, am_params = self._am_clause()
        sql = (
            "SELECT id, name, user_name, bd, am, status, jointime "
            "FROM advertiser WHERE id = %s" + am_clause
        )
        rows = self._query(sql, (aid, *am_params))
        if not rows:
            return {}
        r = rows[0]
        return {
            "id": r.get("id"),
            "name": r.get("name"),
            "user_name": r.get("user_name"),
            "bd": r.get("bd"),
            "am": r.get("am"),
            "status": r.get("status"),
            "status_name": None,
            "jointime": str(r.get("jointime")) if r.get("jointime") is not None else None,
        }

    def _campaign_meta(self, cids) -> dict:
        """批量取 campaign 的 (name, status)，按 str(campaign_id) 索引。"""
        cids = [c for c in cids if c]
        if not cids:
            return {}
        try:
            ids = tuple(int(c) for c in cids)
        except (TypeError, ValueError):
            ids = tuple(cids)
        ph = ",".join(["%s"] * len(ids))
        rows = self._query(f"SELECT id, name, status FROM campaign WHERE id IN ({ph})", ids)
        return {str(r["id"]): {"name": r.get("name"), "status": r.get("status")} for r in rows}

    def _month_closed_totals(self, aid, closed_yms: list) -> dict:
        """report_month 已结月聚合：campaign_id -> {revenue, payout, conversions}。"""
        if not closed_yms:
            return {}
        lo, hi = min(closed_yms), max(closed_yms)
        sql = (
            "SELECT campaign_id, SUM(revenue) rev, SUM(payout) pay, SUM(conversions) conv "
            "FROM report_month WHERE advertiser_id=%s AND date BETWEEN %s AND %s GROUP BY campaign_id"
        )
        rows = self._query_across(sql, (aid, lo, hi))
        return {
            str(r["campaign_id"]): {
                "revenue": self._f(r.get("rev")),
                "payout": self._f(r.get("pay")),
                "conversions": self._f(r.get("conv")),
            }
            for r in rows
        }

    def _hour_fill(self, cids, ds: str, de: str) -> dict:
        """report_hour_new 小 IN 回填完整窗口指标（含缺口月 + 当前进行月 + clicks）。
        campaign_id -> {revenue, payout, clicks, conversions}。scoped 极快。

        关键坑：report_hour_new.campaign_id 是 VARCHAR(64)，必须按**字符串**传入 IN 列表，
        否则 MySQL 类型不匹配会放弃 camp_adv_date_hour 索引、退化为 advertiser_id 全扫描
        （单 advertiser 1200W+ 行，单 campaign 聚合 20s+，整体直接超时）。字符串传入即走
        campaign_id 前缀的 range 扫描（单 campaign 0.05s）。"""
        cids = [c for c in cids if c]
        if not cids:
            return {}
        ids = tuple(str(c) for c in cids)
        ph = ",".join(["%s"] * len(ids))
        sql = (
            f"SELECT campaign_id, SUM(revenue) rev, SUM(payout) pay, "
            f"SUM(clicks) clk, SUM(conversions) conv "
            f"FROM report_hour_new WHERE campaign_id IN ({ph}) "
            f"AND date BETWEEN %s AND %s GROUP BY campaign_id"
        )
        rows = self._query_across(sql, (*ids, ds, de))
        return {
            str(r["campaign_id"]): {
                "revenue": self._f(r.get("rev")),
                "payout": self._f(r.get("pay")),
                "clicks": self._f(r.get("clk")),
                "conversions": self._f(r.get("conv")),
            }
            for r in rows
        }

    def _campaign_daily_kpi(self, params: dict) -> dict:
        """模拟 GET /advertisers/campaign-daily-kpi：旗下 Campaign 快照（含状态名）。

        用 report_hour_new 取「窗口内活跃 campaign」（DISTINCT campaign_id，比全量聚合快得多），
        再 LEFT JOIN campaign 取 status → campaign_status_name。营收/payout 走 report_month
        已结月（缺口月/当前月为近似，仅作兜底；快照营收实际由 /report 汇总提供，见下）。
        """
        aid = params.get("advertiser_id")
        ds = params.get("date_start")
        de = params.get("date_end")
        if not aid or not ds or not de:
            return {"campaigns": [], "total": {}}
        # 枚举该广告主旗下 campaign 名单：用 report_month（已结月，按 advertiser_id 索引，
        # 全广告主仅 2.3W 行）取全量 campaign 名单（快，~0.2s）。严禁对 report_hour_new 按
        # advertiser_id 整段扫描——大广告主 1200W+ 行会 20s+ 超时，拖死取数预算。当前进行月
        # （report_month 不含）的 campaign 几乎无历史营收、不会进入 top-30 快照，故不补全。
        closed = self._query_across(
            "SELECT DISTINCT campaign_id FROM report_month WHERE advertiser_id=%s", (aid,))
        act_ids = sorted({str(r["campaign_id"]) for r in closed})
        meta = self._campaign_meta(act_ids)
        yms = self._ym_range(ds, de)
        cur = self._current_ym()
        closed = [y for y in yms if y < cur]
        mtot = self._month_closed_totals(aid, closed)

        campaigns = []
        tot_rev = tot_pay = 0.0
        for cid in act_ids:
            m = meta.get(cid, {})
            t = mtot.get(cid, {})
            rev = self._f(t.get("revenue"))
            pay = self._f(t.get("payout"))
            conv = self._f(t.get("conversions"))
            tot_rev += rev
            tot_pay += pay
            campaigns.append({
                "campaign_id": int(cid) if str(cid).isdigit() else cid,
                "name": m.get("name"),
                "campaign_status_name": campaign_status_label(m.get("status")),
                "revenue": round(rev, 4),
                "payout": round(pay, 4),
                "profit": round(rev - pay, 4),
                "clicks": 0,
                "conversions": int(conv),
                "total": {
                    "revenue": round(rev, 4), "payout": round(pay, 4),
                    "profit": round(rev - pay, 4), "clicks": 0, "conversions": int(conv),
                },
            })
        return {
            "advertiser_id": aid,
            "total": {
                "revenue": round(tot_rev, 4), "payout": round(tot_pay, 4),
                "profit": round(tot_rev - tot_pay, 4), "clicks": 0, "conversions": 0,
            },
            "campaigns": campaigns,
        }

    def _report(self, params: dict) -> dict:
        """模拟 GET /report：按 dimensions 分发到汇总或日明细。"""
        page = int(params.get("page") or 1)
        page_size = int(params.get("page_size") or 100)
        ds = params.get("date_start")
        de = params.get("date_end")
        if params.get("campaign_ids"):
            return self._report_detail(params, page, page_size, ds, de)
        aid = params.get("advertiser_ids") or params.get("advertiser_id")
        if not aid:
            return {"items": []}
        rows = self._agg_campaigns_by_advertiser(aid, ds, de)
        start = (page - 1) * page_size
        return {"items": rows[start: start + page_size]}

    def _agg_campaigns_by_advertiser(self, aid, ds, de) -> list:
        """混合策略：report_month 已结月排名 + report_hour_new 回填 top-N 完整窗口。

        返回按营收降序的 campaign 汇总列表（含 campaign_name/revenue/payout/profit/
        clicks/conversions/cr/margin），覆盖 analytics 消费的 top 30。
        """
        cache_key = ("agg", str(aid), ds, de)
        if cache_key in self._agg_cache:
            return self._agg_cache[cache_key]
        yms = self._ym_range(ds, de)
        cur = self._current_ym()
        closed = [y for y in yms if y < cur]
        mtot = self._month_closed_totals(aid, closed)  # 已结月营收（排名用，快）
        cands = list(mtot.keys())
        if not cands:
            return []
        names = self._campaign_meta(cands)

        # 先按已结月营收排名，取 top-N 用 report_hour_new 回填完整窗口（含缺口月+当前月+clicks）
        ranked = sorted(
            mtot.items(), key=lambda kv: self._f(kv[1].get("revenue")), reverse=True
        )
        top = [cid for cid, _ in ranked[: self.fill_top_n]]
        fill = self._hour_fill(top, ds, de)

        out = []
        for cid in top:
            f = fill.get(cid) or mtot.get(cid, {})
            rev = self._f(f.get("revenue"))
            pay = self._f(f.get("payout"))
            clk = self._f(f.get("clicks"))
            conv = self._f(f.get("conversions"))
            cr = (conv / clk * 100) if clk > 0 else 0.0
            margin = ((rev - pay) / rev * 100) if rev > 0 else 0.0
            out.append({
                "campaign_id": int(cid) if str(cid).isdigit() else cid,
                "campaign_name": (names.get(cid) or {}).get("name"),
                "revenue": round(rev, 4),
                "payout": round(pay, 4),
                "profit": round(rev - pay, 4),
                "clicks": int(clk),
                "conversions": int(conv),
                "cr": round(cr, 4),
                "margin": round(margin, 4),
            })
        # 回填后按完整营收重排（当前月可能微调名次）
        out.sort(key=lambda x: x["revenue"], reverse=True)
        return out

    def _report_detail(self, params, page, page_size, ds, de) -> dict:
        """report_hour_new 日级明细（scoped campaign_ids，走索引极快）。
        返回按 (campaign_id, date) 聚合的日行，供上层 _monthly_from_daily 聚成月度。缓存。"""
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        if not cids or not ds or not de:
            return {"items": []}
        cids = list(dict.fromkeys(cids))  # 去重
        key = ("det", tuple(cids), ds, de)
        if key in self._agg_cache:
            all_rows = self._agg_cache[key]
        else:
            # report_hour_new.campaign_id 是 VARCHAR(64)，必须按字符串传入 IN 以命中 camp_adv_date_hour 索引
            ids = tuple(str(c) for c in cids)
            ph = ",".join(["%s"] * len(ids))
            sql = (
                f"SELECT campaign_id, date, SUM(revenue) rev, SUM(payout) pay, "
                f"SUM(clicks) clk, SUM(conversions) conv "
                f"FROM report_hour_new WHERE campaign_id IN ({ph}) "
                f"AND date BETWEEN %s AND %s GROUP BY campaign_id, date "
                f"ORDER BY campaign_id, date"
            )
            rows = self._query_across(sql, (*ids, ds, de))
            all_rows = []
            for r in rows:
                rev = self._f(r.get("rev"))
                pay = self._f(r.get("pay"))
                clk = self._f(r.get("clk"))
                conv = self._f(r.get("conv"))
                cr = (conv / clk * 100) if clk > 0 else 0.0
                margin = ((rev - pay) / rev * 100) if rev > 0 else 0.0
                all_rows.append({
                    "campaign_id": int(r["campaign_id"]) if str(r.get("campaign_id")).isdigit() else r.get("campaign_id"),
                    "date": r.get("date"),
                    "revenue": round(rev, 4),
                    "payout": round(pay, 4),
                    "clicks": int(clk),
                    "conversions": int(conv),
                    "cr": round(cr, 4),
                    "margin": round(margin, 4),
                })
            self._agg_cache[key] = all_rows
        start = (page - 1) * page_size
        return {"items": all_rows[start: start + page_size]}

    def _campaigns(self, params: dict) -> dict:
        """模拟 GET /campaigns：按 id 精确回目录（用于补缺失 campaign 的 status / config）。

        campaign 表 status 为数字、status_name 恒空，由调用方 _camp_status_label 映射。
        campaign_detail 取 id/name/advertiser_id/country/cap/click_cap/status/kpi 与线上
        /campaigns 目录对齐。**关键**：campaign 表无 publisher_id 列（多对多经 publisher_campaign），
        且线上 /campaigns 也不回 publisher_id 字段（已实测 7160172 → key 不存在），故这里一致
        不产出该字段，保证 campaign_config 逐字段 0 差异（_pick 两侧都得 None）。
        """
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        if not cids:
            return {"items": []}
        try:
            ids = tuple(int(c) for c in cids)
        except (TypeError, ValueError):
            ids = tuple(cids)
        ph = ",".join(["%s"] * len(ids))
        rows = self._query(
            f"SELECT id, name, advertiser_id, country, cap, click_cap, "
            f"status, kpi FROM campaign WHERE id IN ({ph})", ids)
        return {
            "items": [
                {
                    "id": r.get("id"),
                    "campaign_id": r.get("id"),
                    "name": r.get("name"),
                    "campaign_name": r.get("name"),
                    "advertiser_id": r.get("advertiser_id"),
                    "country": r.get("country"),
                    "cap": self._f(r.get("cap")),
                    "click_cap": r.get("click_cap"),
                    "status": r.get("status"),
                    "status_name": campaign_status_label(r.get("status")),
                    "kpi": r.get("kpi"),
                }
                for r in rows
            ]
        }

    def _campaign_kpi_trend(self, params: dict) -> dict:
        """模拟 GET /campaign-kpi-trend：单/多 campaign 日级趋势（短周期 / 回退路径用）。

        读 report_hour_new 日级明细（scoped，走索引），与线上接口逐字段吻合。
        字段对齐：revenue/payout/clicks/conversions/cvr/reject_count/reject_rate/
        completed/margin_rate/profit + 顶栏 total_*。
        """
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        ds = params.get("date_start")
        de = params.get("date_end")
        if not cids:
            return {"items": [], "total_revenue": 0, "total_clicks": 0,
                    "total_conversions": 0, "total_profit": 0}
        # campaign_detail 调它时只传 campaign_ids、不传日期 → 与线上接口一致回退「近 7 天
        # 结束于昨天」的默认窗口（已实测线上 kpi-trend 无日期 = 10-09..10-15 共 7 天），否则
        # 返回空会让 campaign_detail 的 kpi_trend 缺数。
        if not ds or not de:
            ds, de = self._recent_window(7)
        # report_hour_new.campaign_id 是 VARCHAR(64)，必须按字符串传入 IN 以命中 camp_adv_date_hour 索引
        ids = tuple(str(c) for c in cids)
        ph = ",".join(["%s"] * len(ids))
        sql = (
            f"SELECT campaign_id, date, SUM(revenue) rev, SUM(payout) pay, "
            f"SUM(clicks) clk, SUM(conversions) conv, SUM(reject_count) rj "
            f"FROM report_hour_new WHERE campaign_id IN ({ph}) "
            f"AND date BETWEEN %s AND %s GROUP BY campaign_id, date ORDER BY campaign_id, date"
        )
        rows = self._query(sql, (*ids, ds, de))
        out = []
        t_rev = t_pay = t_clk = t_conv = 0.0
        for r in rows:
            rev = self._f(r.get("rev"))
            pay = self._f(r.get("pay"))
            clk = int(self._f(r.get("clk")))
            conv = int(self._f(r.get("conv")))
            rj = int(self._f(r.get("rj")))
            # 与线上接口对齐：revenue/payout 先 2 位四舍五入，margin_rate/cvr/profit 均按 2 位输出
            rev_r = round(rev, 2)
            pay_r = round(pay, 2)
            cvr = (conv / clk * 100) if clk > 0 else 0.0
            margin_rate = ((rev_r - pay_r) / rev_r * 100) if rev_r > 0 else 0.0
            out.append({
                "time_label": r.get("date"),
                "revenue": rev_r,
                "payout": pay_r,
                "clicks": clk,
                "conversions": conv,
                "cvr": round(cvr, 2),
                "reject_count": rj,
                "reject_rate": round(rj / conv * 100, 2) if conv else 0.0,
                "completed": 0.0,
                "margin_rate": round(margin_rate, 2),
                "profit": round(rev_r - pay_r, 2),
            })
            t_rev += rev_r; t_pay += pay_r; t_clk += clk; t_conv += conv
        return {
            "items": out,
            "total_revenue": round(t_rev, 2),
            "total_clicks": int(t_clk),
            "total_conversions": round(t_conv, 2),
            "total_profit": round(t_rev - t_pay, 2),
        }

    # ----- publisher / quality / ctit 实现（publisher_deepdive + campaign_detail 所需） -----

    @staticmethod
    def _ctit_buckets() -> list:
        """CTIT/ETIT 分桶（秒区间），与线上接口 30 桶对齐。"""
        return [
            ("5s", 0, 5), ("5-30s", 5, 30), ("30-60s", 30, 60),
            ("1-5min", 60, 300), ("5-30min", 300, 1800), ("30-60min", 1800, 3600),
            ("1-2h", 3600, 7200), ("2-3h", 7200, 10800), ("3-4h", 10800, 14400),
            ("4-5h", 14400, 18000), ("5-6h", 18000, 21600), ("6-7h", 21600, 25200),
            ("7-8h", 25200, 28800), ("8-9h", 28800, 32400), ("9-10h", 32400, 36000),
            ("10-11h", 36000, 39600), ("11-12h", 39600, 43200), ("12-13h", 43200, 46800),
            ("13-14h", 46800, 50400), ("14-15h", 50400, 54000), ("15-16h", 54000, 57600),
            ("16-17h", 57600, 61200), ("17-18h", 61200, 64800), ("18-19h", 64800, 68400),
            ("19-20h", 68400, 72000), ("20-21h", 72000, 75600), ("21-22h", 75600, 79200),
            ("22-23h", 79200, 82800), ("23-24h", 82800, 86400), ("24h+", 86400, 10 ** 12),
        ]

    def _recent_window(self, days: int = 7):
        """近 N 天窗口，但与线上接口对齐：结束于「昨天」（线上 quality/kpi-trend 默认窗口
        不含当天，因当天为部分日）。host today=T 时返回 [T-days, T-1]。"""
        now = datetime.now()
        de = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        ds = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        return ds, de

    def _publishers(self, params: dict) -> dict:
        """模拟 GET /publishers：publisher 表直出（平台隔离，按 self.database）。

        注意：analytics 仅取 /publishers 首页（page_size=100）构建 pub_map 查 status/margin，
        但 /campaign-quality/publisher 会返回全量（>100）渠道。若按分页只回首页，pub_map 不全
        → 部分渠道 status 缺失。故这里返回全量（按 id 排序），保证 pub_map 完整。
        """
        rows = self._query(
            "SELECT id, name, user_name, status, margin, bd, pm, jointime FROM publisher ORDER BY id")
        total = len(rows)
        items = []
        for r in rows:
            items.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "user_name": r.get("user_name"),
                "status": r.get("status"),
                "status_name": "",
                "margin": self._f(r.get("margin")),
                "bd": r.get("bd"),
                "pm": r.get("pm"),
                "jointime": str(r.get("jointime")) if r.get("jointime") is not None else None,
            })
        return {"total": total, "page": 1, "page_size": total, "items": items}

    def _quality_hour_agg(self, dim: str, dim_value, ds: str, de: str) -> dict:
        """report_hour_new 按维度聚合质量时序（可靠字段）。返回 {date:{conversions,postback_conversions,clicks,reject_count}}。"""
        if dim == "campaign_id":
            col, val = "campaign_id", str(dim_value)
        else:
            col, val = "publisher_id", int(dim_value)
        sql = (
            f"SELECT date, SUM(conversions) conv, SUM(postback_conversions) pb, "
            f"SUM(clicks) clk, SUM(reject_count) rj FROM report_hour_new "
            f"WHERE {col} = %s AND date BETWEEN %s AND %s GROUP BY date ORDER BY date"
        )
        rows = self._query(sql, (val, ds, de))
        out = {}
        for r in rows:
            out[r["date"]] = {
                "conversions": self._f(r.get("conv")),
                "postback_conversions": self._f(r.get("pb")),
                "clicks": int(self._f(r.get("clk"))),
                "reject_count": int(self._f(r.get("rj"))),
            }
        return out

    def _quality_item(self, label, conv, pb, clk, rj, q1, q2):
        conv = float(conv); pb = float(pb); clk = float(clk); rj = float(rj)
        return {
            "time_label": label,
            "conversions": round(conv, 2),
            "postback_conversions": round(pb, 2),
            "clicks": int(clk),
            "q1_count": int(q1), "q1_rate": round(q1 / conv * 100, 2) if conv else 0.0,
            "q2_count": int(q2), "q2_rate": round(q2 / conv * 100, 2) if conv else 0.0,
            "reject_count": int(rj), "reject_rate": round(rj / conv * 100, 2) if conv else None,
            "completed": 0.0,
        }

    def _quality_total(self, conv, pb, clk, rj, q1, q2):
        conv = float(conv); pb = float(pb); clk = float(clk); rj = float(rj)
        return {
            "conversions": round(conv, 2),
            "postback_conversions": round(pb, 2),
            "clicks": int(clk),
            "q1_count": int(q1), "q1_rate": round(q1 / conv * 100, 2) if conv else 0.0,
            "q2_count": int(q2), "q2_rate": round(q2 / conv * 100, 2) if conv else 0.0,
            "reject_count": int(rj), "reject_rate": round(rj / conv * 100, 2) if conv else None,
            "completed": 0.0,
        }

    def _event_quality(self, cid, q1_event, q2_event, ds, de) -> dict:
        """event_postback_info 按 event_name 匹配 q1/q2 事件，统计每日起因数（best-effort）。"""
        try:
            sql = ("SELECT date, event_name, COUNT(*) n FROM event_postback_info "
                   "WHERE campaign_id=%s AND date BETWEEN %s AND %s GROUP BY date, event_name")
            rows = self._query(sql, (cid, ds, de))
        except Exception:
            return {}
        out = {}
        for r in rows:
            en = (r.get("event_name") or "")
            n = int(r.get("n") or 0)
            d = r["date"]
            bucket = out.setdefault(d, {"q1": 0, "q2": 0})
            if q1_event and en == q1_event:
                bucket["q1"] += n
            elif q2_event and en == q2_event:
                bucket["q2"] += n
        return out

    def _campaign_quality(self, params: dict) -> dict:
        """模拟 GET /campaign-quality（campaign 级日质量时序）。q1/q2 尽力补。"""
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        if not cids:
            return {"items": [], "total": {}, "q1_event": None, "q2_event": None}
        cid = cids[0]
        ds, de = self._recent_window(7)
        agg = self._quality_hour_agg("campaign_id", cid, ds, de)
        q1_event = q2_event = None
        try:
            r = self._query("SELECT campaign_q1_event, campaign_q2_event FROM campaign WHERE id=%s", (int(cid),))
            if r:
                q1_event, q2_event = r[0].get("campaign_q1_event"), r[0].get("campaign_q2_event")
        except Exception:
            pass
        ev = self._event_quality(str(cid), q1_event, q2_event, ds, de) if (q1_event or q2_event) else {}
        items = []
        tc = tp = tk = tr = tq1 = tq2 = 0.0
        for d in sorted(agg):
            a = agg[d]
            ec = ev.get(d, {})
            conv, pb, clk, rj = a["conversions"], a["postback_conversions"], a["clicks"], a["reject_count"]
            q1, q2 = ec.get("q1", 0), ec.get("q2", 0)
            items.append(self._quality_item(d, conv, pb, clk, rj, q1, q2))
            tc += conv; tp += pb; tk += clk; tr += rj; tq1 += q1; tq2 += q2
        total = self._quality_total(tc, tp, tk, tr, tq1, tq2)
        return {"items": items, "total": total, "q1_event": q1_event, "q2_event": q2_event}

    def _publisher_quality(self, params: dict) -> dict:
        """模拟 GET /campaign-quality/publisher（全渠道质量，默认近 7 天）。"""
        ds, de = self._recent_window(7)
        sql = (
            "SELECT publisher_id, date, SUM(conversions) conv, SUM(postback_conversions) pb, "
            "SUM(clicks) clk, SUM(reject_count) rj FROM report_hour_new "
            "WHERE date BETWEEN %s AND %s GROUP BY publisher_id, date"
        )
        rows = self._query(sql, (ds, de))
        by_pub = {}
        for r in rows:
            pid = r["publisher_id"]
            by_pub.setdefault(pid, {})[r["date"]] = {
                "conversions": self._f(r.get("conv")),
                "postback_conversions": self._f(r.get("pb")),
                "clicks": int(self._f(r.get("clk"))),
                "reject_count": int(self._f(r.get("rj"))),
            }
        pub_rows = self._query("SELECT id, name, status FROM publisher")
        pub_meta = {r["id"]: {"name": r.get("name"), "status": r.get("status")} for r in pub_rows}
        items = []
        for pid, dates in by_pub.items():
            meta = pub_meta.get(pid, {})
            per_date = {}
            tc = tp = tk = tr = 0.0
            for d, a in dates.items():
                per_date[d] = self._quality_item(d, a["conversions"], a["postback_conversions"], a["clicks"], a["reject_count"], 0, 0)
                tc += a["conversions"]; tp += a["postback_conversions"]; tk += a["clicks"]; tr += a["reject_count"]
            total = self._quality_total(tc, tp, tk, tr, 0, 0)
            items.append({
                "publisher_id": pid,
                "publisher_name": meta.get("name"),
                "publisher_status": meta.get("status"),
                "total": total,
                **per_date,
            })
        return {"items": items}

    @staticmethod
    def _parse_dt(value):
        """容错解析时间：网盟 install_time 实际为 'YYYY:MM:DD HH:MM:SS'（冒号分隔日期），
        与标准 'YYYY-MM-DD' 并存，两种都要支持；可选微秒；也接受已解析的 datetime 对象。"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        s = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S",
                   "%Y-%m-%d %H:%M:%S.%f", "%Y:%m:%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    # 噪声事件：APP_OPEN 是安装后用户「打开 App」的常规行为，ETIT（事件→安装间隔）对其
    # 无意义（用户可能数天后才打开），CTIT/ETIT 仅统计转化类事件。
    _CTIT_NOISE_EVENTS = ("APP_OPEN",)

    def _ctit_etit(self, params: dict) -> dict:
        """模拟 GET /campaign-ctit-etit：CTIT/ETIT 分布。

        - CTIT = 安装时间 − 点击时间（需 click 表；melocpi 有，seanear 无 click 表 → 委托 API）。
        - ETIT = 事件时间 − 安装时间（取 event_postback_info 的 create_time − install_time，
          **不依赖 click 表**，故即便无点击匹配也能算）。

        ⚠️ 已知偏差（2026-10-15 实测）：线上 /campaign-ctit-etit **忽略 date 参数**（固定全量、
        且对部分 campaign 退化为只吐 1 个桶），并非可靠对照基准。本实现按真实数据计算，结果
        以 MySQL 为准；ds/de 缺省时走全量，与线上「忽略日期」行为对齐。
        """
        cid = str(params.get("campaign_id") or params.get("campaign_ids") or "")
        if not cid:
            return {"ctit": {}, "etit": {}}
        ds = params.get("date_start")
        de = params.get("date_end")
        ctit = {b[0]: 0 for b in self._ctit_buckets()}
        etit = {b[0]: 0 for b in self._ctit_buckets()}
        try:
            # ETIT：直接从 event_postback_info 算（install_time → create_time），排除噪声事件
            sql_e = (
                "SELECT install_time, create_time, event_name FROM event_postback_info "
                "WHERE campaign_id=%s"
            )
            eargs = [cid]
            if ds and de:
                sql_e += " AND date BETWEEN %s AND %s"
                eargs += [ds, de]
            sql_e += " AND install_time IS NOT NULL AND install_time <> ''"
            for r in self._query(sql_e, tuple(eargs)):
                it = self._parse_dt(r.get("install_time"))
                et = self._parse_dt(r.get("create_time"))
                if not it or not et:
                    continue
                if r.get("event_name") in self._CTIT_NOISE_EVENTS:
                    continue
                d = max(0, int((et - it).total_seconds()))
                lab = self._bucket_label(d)
                etit[lab] = etit.get(lab, 0) + 1
            # CTIT：install_time − click_time（需 click 表关联）
            sql_c = (
                "SELECT e.install_time, c.create_time clk_time "
                "FROM event_postback_info e JOIN click c ON e.click_uuid = c.clickid "
                "WHERE e.campaign_id=%s"
            )
            cargs = [cid]
            if ds and de:
                sql_c += " AND e.date BETWEEN %s AND %s"
                cargs += [ds, de]
            sql_c += " AND e.install_time IS NOT NULL AND e.install_time <> ''"
            for r in self._query(sql_c, tuple(cargs)):
                it = self._parse_dt(r.get("install_time"))
                clk = r.get("clk_time")
                clk = self._parse_dt(clk) if not isinstance(clk, (int, float)) else datetime.fromtimestamp(clk)
                if not it or not clk:
                    continue
                d = max(0, int((it - clk).total_seconds()))
                lab = self._bucket_label(d)
                ctit[lab] = ctit.get(lab, 0) + 1
        except Exception:
            # seanear 等无 click 表或 event_postback_info 结构不同 → 委托真实 API
            if self._api_fallback is None:
                from tess_backend.data_connector import TeensingDataConnector, get_platform_base_url, _db_data_token
                self._api_fallback = TeensingDataConnector(base_url=get_platform_base_url(self.platform_id))
            return self._api_fallback.api_get("/campaign-ctit-etit", params, None, None)
        return {"ctit": ctit, "etit": etit}

    def _bucket_label(self, secs: int) -> str:
        for lab, lo, hi in self._ctit_buckets():
            if lo <= secs < hi:
                return lab
        return "24h+"

    def _am_clause(self) -> tuple:
        """运营级隔离：operator_id 给定时，仅返回该运营（am）拥有的广告主。"""
        if self.operator_id:
            try:
                return " AND am = %s", (int(self.operator_id),)
            except (TypeError, ValueError):
                return " AND am = %s", (self.operator_id,)
        return "", ()
