"""P5 · 直连网盟 MySQL 取数连接器（MysqlDataConnector）。

替代 TeensingDataConnector 经 HTTP 调 /report 等接口，直接查网盟 MySQL
（即 tess_ai_pb 已连接的 172.22.0.6 那台）的预聚合报表表 + 实体表。

设计要点：
- 实现与 TeensingDataConnector 同一套 api_get(path, params, token, timeout) 接口，
  返回 dict 形状与 Teensing HTTP 接口**完全一致**，因此 18 种 analysis_type 取数逻辑
  一行不动，仅底层数据源切换（见 data_connector.get_data_connector 的 mysql 分支）。
- 平台级隔离：按 platform_id 选库（Melodong→melocpi / seanear→seanear）。
- 运营级隔离：operator_id 给定时，advertiser 维度查询注入 WHERE am = operator_id。
- 读预聚合表（report_campaign 等），不重算 postback；profit = revenue - payout（已确认无调整项）。
- 切换开关：TESS_DATA_CONNECTOR=mysql 启用；默认 teensing（HTTP 接口），零风险回退。
"""

from __future__ import annotations

import json
import logging
import os
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
        # 重聚合查询结果进程内缓存，避免长周期下钻对同一个 (campaign_ids,ds,de)
        # 重复跑几十次分页查询（analytics 会并发拉 30 页，各自其实是同一全量结果）。
        self._agg_cache: dict = {}
        # 试点期跨库回退：默认开（=1）。当默认库（按 platform_id 选）查不到某广告主时，
        # 自动尝试 TESS_MYSQL_DATABASES 中其它库（如 Melodong 运营误查到 seanear 数据）。
        # 上线前建议置 0，避免跨平台数据串档（运营隔离由 platform_id 决定）。
        self.cross_db = str(os.getenv("TESS_MYSQL_CROSS_DB", "1") or "1").strip() not in (
            "0", "false", "no", "off"
        )

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
        """执行查询，返回 list[dict]（DictCursor）。库 = self.database。

        子类 / 单测可重写以注入假数据（见 tests/test_mysql_connector.py 的 Fake 实现）。
        """
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

    def self_check(
        self, advertiser_id: int = 1000853,
        ds: str = "2026-07-01", de: str = "2026-10-31",
    ) -> dict:
        """只读自检：确认连通性 + 每个库里该广告主的数据分布 + date 格式。

        返回 dict 供 verify_mysql.py 打印；生产排障一眼看出「连没连上 / 数据在哪个库 /
        date 列是不是 YYYY-MM-DD（决定 BETWEEN 是否生效）」。不抛异常。
        """
        out: dict = {
            "host": self.host, "port": self.port, "user": self.user,
            "default_db": self.database, "databases": self.databases, "cross_db": self.cross_db,
            "dbs": {},
        }
        for name, db in self.databases.items():
            entry = {
                "db": db, "reachable": False, "error": None,
                "total_rows": None, "window_rows": None, "date_sample": [],
            }
            try:
                r = self._query_db(
                    db, "SELECT COUNT(*) AS c FROM report_campaign WHERE advertiser_id=%s",
                    (advertiser_id,),
                )
                entry["total_rows"] = r[0]["c"] if r else None
                r2 = self._query_db(
                    db,
                    "SELECT COUNT(*) AS c FROM report_campaign "
                    "WHERE advertiser_id=%s AND date BETWEEN %s AND %s",
                    (advertiser_id, ds, de),
                )
                entry["window_rows"] = r2[0]["c"] if r2 else None
                r3 = self._query_db(
                    db,
                    "SELECT DISTINCT date FROM report_campaign WHERE advertiser_id=%s LIMIT 5",
                    (advertiser_id,),
                )
                entry["date_sample"] = [x["date"] for x in r3] if r3 else []
                entry["reachable"] = True
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {e}"
            out["dbs"][name] = entry
        return out

    @staticmethod
    def _f(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # ----- 路由：模拟 Teensing HTTP 接口返回形状 -----

    def api_get(
        self,
        path: str,
        params: Optional[dict] = None,
        token: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> dict:
        """公开透传：与 TeensingDataConnector.api_get 同签名，返回已「解包」的业务 dict。

        timeout 仅用于兼容调用方传入（此处透传给连接超时），不缓存连接。
        """
        params = params or {}
        if path == "/advertisers/campaign-daily-kpi":
            return self._campaign_daily_kpi(params)
        if path.startswith("/advertisers/"):
            aid = path.split("/advertisers/", 1)[1]
            return self._advertiser_profile(aid)
        if path == "/report":
            return self._report(params)
        if path == "/campaigns":
            return self._campaigns(params)
        if path == "/campaign-kpi-trend":
            return self._campaign_kpi_trend(params)
        # 未实现的路径：返回空结构，让调用方容错（不抛异常、不阻断整轮分析）
        logger.warning("MysqlDataConnector 未实现的路径 %s，返回空结构", path)
        return {"items": []}

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
            # advertiser 表无 status_name 列，与 HTTP 接口同行为 None
            "status_name": None,
            "jointime": str(r.get("jointime")) if r.get("jointime") is not None else None,
        }

    def _campaign_daily_kpi(self, params: dict) -> dict:
        """模拟 GET /advertisers/campaign-daily-kpi：旗下 Campaign 快照（含状态名）。

        用 report_campaign 按 campaign 聚合 [ds,de] 全区间（天然覆盖窗口内活跃 campaign），
        并 LEFT JOIN campaign 取 status → campaign_status_name，彻底消除 status 全 null。
        """
        aid = params.get("advertiser_id")
        ds = params.get("date_start")
        de = params.get("date_end")
        if not aid or not ds or not de:
            return {"campaigns": [], "total": {}}
        sql = (
            "SELECT rc.campaign_id AS campaign_id, c.name AS name, c.status AS status, "
            "SUM(rc.revenue) AS revenue, SUM(rc.payout) AS payout, "
            "SUM(rc.clicks) AS clicks, SUM(rc.conversions) AS conversions "
            "FROM report_campaign rc LEFT JOIN campaign c ON c.id = rc.campaign_id "
            "WHERE rc.advertiser_id = %s AND rc.date BETWEEN %s AND %s "
            "GROUP BY rc.campaign_id, c.name, c.status"
        )
        rows = self._query_across(sql, (aid, ds, de))
        campaigns = []
        tot_rev = tot_pay = tot_clk = tot_conv = 0.0
        for r in rows:
            rev = self._f(r.get("revenue"))
            pay = self._f(r.get("payout"))
            clk = self._f(r.get("clicks"))
            conv = self._f(r.get("conversions"))
            tot_rev += rev
            tot_pay += pay
            tot_clk += clk
            tot_conv += conv
            campaigns.append({
                "campaign_id": r.get("campaign_id"),
                "name": r.get("name"),
                "campaign_status_name": campaign_status_label(r.get("status")),
                "revenue": rev,
                "payout": pay,
                "profit": rev - pay,
                "clicks": clk,
                "conversions": conv,
                "total": {
                    "revenue": rev,
                    "payout": pay,
                    "profit": rev - pay,
                    "clicks": clk,
                    "conversions": conv,
                },
            })
        return {
            "advertiser_id": aid,
            "total": {
                "revenue": tot_rev,
                "payout": tot_pay,
                "profit": tot_rev - tot_pay,
                "clicks": tot_clk,
                "conversions": tot_conv,
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
        return {"items": rows[start : start + page_size]}

    def _agg_campaigns_by_advertiser(self, aid, ds, de) -> list:
        """report_campaign 按 campaign 聚合（全区间汇总），带 campaign 名称。缓存。"""
        key = ("adv", str(aid), ds, de)
        if key in self._agg_cache:
            return self._agg_cache[key]
        sql = (
            "SELECT rc.campaign_id AS campaign_id, c.name AS campaign_name, "
            "SUM(rc.revenue) AS revenue, SUM(rc.payout) AS payout, "
            "SUM(rc.clicks) AS clicks, SUM(rc.conversions) AS conversions "
            "FROM report_campaign rc LEFT JOIN campaign c ON c.id = rc.campaign_id "
            "WHERE rc.advertiser_id = %s AND rc.date BETWEEN %s AND %s "
            "GROUP BY rc.campaign_id, c.name "
            "ORDER BY SUM(rc.revenue) DESC"
        )
        rows = self._query_across(sql, (aid, ds, de))
        out = []
        for r in rows:
            rev = self._f(r.get("revenue"))
            pay = self._f(r.get("payout"))
            clk = self._f(r.get("clicks"))
            conv = self._f(r.get("conversions"))
            cr = (conv / clk * 100) if clk > 0 else 0.0
            margin = ((rev - pay) / rev * 100) if rev > 0 else 0.0
            out.append({
                "campaign_id": r.get("campaign_id"),
                "campaign_name": r.get("campaign_name"),
                "revenue": rev,
                "payout": pay,
                "profit": rev - pay,
                "clicks": clk,
                "conversions": conv,
                "cr": round(cr, 4),
                "margin": round(margin, 4),
            })
        self._agg_cache[key] = out
        return out

    def _report_detail(self, params, page, page_size, ds, de) -> dict:
        """report_campaign 按 campaign,date 的日明细（供长周期本地按月聚合）。缓存。"""
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        if not cids or not ds or not de:
            return {"items": []}
        key = ("det", tuple(cids), ds, de)
        if key in self._agg_cache:
            all_rows = self._agg_cache[key]
        else:
            placeholders = ",".join(["%s"] * len(cids))
            sql = (
                "SELECT campaign_id, date, revenue, payout, clicks, conversions "
                f"FROM report_campaign WHERE campaign_id IN ({placeholders}) "
                "AND date BETWEEN %s AND %s ORDER BY campaign_id, date"
            )
            rows = self._query_across(sql, (*cids, ds, de))
            all_rows = []
            for r in rows:
                rev = self._f(r.get("revenue"))
                pay = self._f(r.get("payout"))
                clk = self._f(r.get("clicks"))
                conv = self._f(r.get("conversions"))
                cr = (conv / clk * 100) if clk > 0 else 0.0
                margin = ((rev - pay) / rev * 100) if rev > 0 else 0.0
                all_rows.append({
                    "campaign_id": r.get("campaign_id"),
                    "date": r.get("date"),
                    "revenue": rev,
                    "payout": pay,
                    "clicks": clk,
                    "conversions": conv,
                    "cr": round(cr, 4),
                    "margin": round(margin, 4),
                })
            self._agg_cache[key] = all_rows
        start = (page - 1) * page_size
        return {"items": all_rows[start : start + page_size]}

    def _campaigns(self, params: dict) -> dict:
        """模拟 GET /campaigns：按 id 精确回目录（用于补缺失 campaign 的 status）。

        campaign 表 status 为数字、status_name 恒空，由调用方 _camp_status_label 映射。
        """
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        if not cids:
            return {"items": []}
        placeholders = ",".join(["%s"] * len(cids))
        sql = f"SELECT id, name, status FROM campaign WHERE id IN ({placeholders})"
        rows = self._query(sql, tuple(cids))
        return {
            "items": [
                {
                    "campaign_id": r.get("id"),
                    "status": r.get("status"),
                    "campaign_name": r.get("name"),
                }
                for r in rows
            ]
        }

    def _campaign_kpi_trend(self, params: dict) -> dict:
        """模拟 GET /campaign-kpi-trend：单/多 campaign 日级趋势（短周期 / 回退路径用）。"""
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        ds = params.get("date_start")
        de = params.get("date_end")
        if not cids or not ds or not de:
            return {"items": []}
        placeholders = ",".join(["%s"] * len(cids))
        sql = (
            "SELECT campaign_id, date, revenue, payout, clicks, conversions "
            f"FROM report_campaign WHERE campaign_id IN ({placeholders}) "
            "AND date BETWEEN %s AND %s ORDER BY campaign_id, date"
        )
        rows = self._query_across(sql, (*cids, ds, de))
        out = []
        for r in rows:
            rev = self._f(r.get("revenue"))
            pay = self._f(r.get("payout"))
            clk = self._f(r.get("clicks"))
            conv = self._f(r.get("conversions"))
            cvr = (conv / clk * 100) if clk > 0 else 0.0
            margin_rate = ((rev - pay) / rev * 100) if rev > 0 else 0.0
            out.append({
                "time_label": r.get("date"),
                "revenue": rev,
                "payout": pay,
                "clicks": clk,
                "conversions": conv,
                "cvr": round(cvr, 4),
                "margin_rate": round(margin_rate, 4),
            })
        return {"items": out}

    def _am_clause(self) -> tuple:
        """运营级隔离：operator_id 给定时，仅返回该运营（am）拥有的广告主。"""
        if self.operator_id:
            try:
                return " AND am = %s", (int(self.operator_id),)
            except (TypeError, ValueError):
                return " AND am = %s", (self.operator_id,)
        return "", ()
