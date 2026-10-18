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


# 路径 -> 实际查的表（语义层 / 业务词典）：运营在「数据依据」面板看到的「查了哪些表」就来自这里。
# 与 api_get 里 served_routes 记录的 path 一一对应；未知 path 不映射（留空，不臆造表名）。
PATH_TABLES: dict = {
    "/advertisers/campaign-daily-kpi": ["report_month", "report_hour_new"],
    "/advertisers": ["advertiser"],
    "/advertisers/": ["advertiser", "campaign"],  # 单档案（前缀匹配）
    "/report": ["report_day", "report_hour_new", "report_month"],
    "/report/month": ["report_month", "advertiser"],
    "/campaigns": ["campaign"],
    "/campaign-kpi-trend": ["report_hour_new"],
    "/publishers": ["publisher"],
    "/campaign-quality/publisher": ["report_hour_new"],
    "/campaign-quality": ["report_hour_new", "event_postback_info"],
    "/campaign-ctit-etit": ["report_hour_new", "event_postback_info", "click"],
    "/overview/ranking": ["report_month"],
    "/overview/ranking/fluctuation": ["report_month"],
    "/overview/daily-kpi": ["report_day"],
    "/global-settings": ["global_setting"],
    "/advertiser-publisher-pkg-maps": ["advertiser_publisher_pkg_map"],
    "/mapping-publisher-channels": ["mapping_publisher_channel"],
    "/publisher-campaign-blocks": ["publisher_campaign_block", "system_publisher_campaign_block"],
    "/replace-channels": ["replace_channel"],
    "/users/options": ["user"],
    "/overview/ranking/anomaly-warning": ["ad_traffic_warning_log", "publisher_warning"],
    "/campaign-detail": ["campaign", "advertiser", "report_hour_new"],
    "/campaign-compare": ["report_hour_new", "campaign"],
}


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
        fill_top_n: int = 100,
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
        # 用 report_hour_new 回填「完整窗口」的 top-N campaign（覆盖实际消费的 top 30，并留余量
        # 给 /report 默认 page_size=100）。小时级明细 IN 查询耗时随 N 近似线性：实测 N=500≈5.5s、
        # N=100≈1.1s、N=30≈0.3s；取 100 在不回归 /report 翻页的前提下把耗时压到 1s 级。
        self.fill_top_n = int(fill_top_n or 100)
        # 重聚合查询结果进程内缓存，避免长周期下钻对同一 (campaign_ids,ds,de) 重复跑分页查询。
        self._agg_cache: dict = {}
        # 跨库回退：默认关（=0，生产默认）。仅当默认库（按 platform_id 选）查不到时，
        # 才允许自动尝试 TESS_MYSQL_DATABASES 中其它库。开启会导致平台串档
        # （如 seanear 的榜单借到 Melodong 的 campaign/利润）——运营隔离由 platform_id 决定，
        # 不应被回退掩盖。需要时用 TESS_MYSQL_CROSS_DB=1 临时开启。
        self.cross_db = str(os.getenv("TESS_MYSQL_CROSS_DB", "0") or "0").strip() not in (
            "0", "false", "no", "off"
        )
        # 路由审计：记录每个被调用的 path 命中 MySQL（mysql 直连模式下只会是 mysql，
        # 绝不回退 API，保证「全部落到 MySQL 直连」）。
        self.served_routes: list = []

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

        已实现的路径由 MySQL 直出：
          /advertisers, /advertisers/{id}, /report, /report/month, /campaigns, /campaign-kpi-trend,
          /campaign-detail, /replace-channels, /publishers, /campaign-quality,
          /campaign-quality/publisher, /campaign-ctit-etit, /advertisers/campaign-daily-kpi,
          /advertiser-publisher-pkg-maps, /mapping-publisher-channels, /publisher-campaign-blocks,
          /replace-channels, /campaign-compare,
          /users/options, /overview/ranking, /overview/ranking/fluctuation,
          /overview/daily-kpi, /overview/ranking/anomaly-warning, /global-settings。
        未实现的路径**显式报错**（绝不回退 API）：由上游 _safe_api_get 捕获并转为 error 字段，
        保证「全部落到 MySQL 直连」，绝不静默返回空或部分/截断的 API 数据导致对账口径偏差。
        每次调用都记一笔 served_routes（mode 恒为 mysql）。
        """
        params = params or {}
        if path == "/advertisers/campaign-daily-kpi":
            self.served_routes.append((path, "mysql"))
            return self._campaign_daily_kpi(params)
        if path == "/advertisers":
            # 广告主目录（列表），区别于 /advertisers/{aid} 单档案
            return self._mysql_only(path, self._advertisers_list, params, token, timeout)
        if path.startswith("/advertisers/"):
            self.served_routes.append((path, "mysql"))
            aid = path.split("/advertisers/", 1)[1]
            return self._advertiser_profile(aid)
        if path == "/report":
            self.served_routes.append((path, "mysql"))
            return self._report(params)
        if path == "/report/month":
            self.served_routes.append((path, "mysql"))
            return self._report_month(params)
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
        if path == "/campaign-quality/publisher/channels":
            self.served_routes.append((path, "mysql"))
            return self._channel_quality(params)
        if path == "/campaign-ctit-etit":
            self.served_routes.append((path, "mysql"))
            return self._ctit_etit(params)
        # ----- 平台级榜单 / 主数据（dashboard 类，用预聚合表 report_month/report_day，秒级） -----
        if path == "/overview/ranking":
            return self._mysql_only(path, self._overview_ranking, params, token, timeout)
        if path == "/overview/ranking/fluctuation":
            return self._mysql_only(path, self._overview_fluctuation, params, token, timeout)
        if path == "/overview/daily-kpi":
            return self._mysql_only(path, self._daily_kpi, params, token, timeout)
        if path == "/global-settings":
            return self._mysql_only(path, self._global_settings, params, token, timeout)
        # ----- 配置 / 映射 / 规则（原 API 兜底路径，现直连 MySQL 配置表） -----
        if path == "/advertiser-publisher-pkg-maps":
            return self._mysql_only(path, self._adv_pub_pkg_maps, params, token, timeout)
        if path == "/mapping-publisher-channels":
            return self._mysql_only(path, self._mapping_publisher_channels, params, token, timeout)
        if path == "/publisher-campaign-blocks":
            return self._mysql_only(path, self._publisher_campaign_blocks, params, token, timeout)
        if path == "/replace-channels":
            return self._mysql_only(path, self._replace_channels, params, token, timeout)
        if path == "/users/options":
            return self._mysql_only(path, self._users_options, params, token, timeout)
        if path == "/overview/ranking/anomaly-warning":
            return self._mysql_only(path, self._anomaly_warning, params, token, timeout)
        if path == "/campaign-detail":
            return self._mysql_only(path, self._campaign_detail, params, token, timeout)
        if path == "/campaign-compare":
            return self._mysql_only(path, self._campaign_compare, params, token, timeout)
        # 未实现：MySQL 直连模式下绝不回退 API。显式报错，由上游 _safe_api_get 捕获转为
        # error 字段（Gatekeeper 熔断 INCONCLUSIVE），避免静默返回空或部分 API 数据。
        raise NotImplementedError(
            f"MysqlDataConnector 未实现路径 {path!r}；全部取数须走 MySQL 直连，"
            f"请为其在 mysql_connector.py 增加对应的 _<name> 方法并在 api_get 路由。"
        )

    # ----- 数据依据（provenance）透传：供前端「查了哪张表/哪些条件」折叠面板 -----
    def build_provenance(self, params: Optional[dict] = None,
                         route_source: Optional[str] = None,
                         date_or_month: Optional[str] = None) -> dict:
        """汇总本次请求命中的取数路径与表，生成可透传给前端的依据块。

        纯只读、纯确定性（served_routes 已逐笔记录 mysql/api 命中），LLM 不碰，
        符合三层死锁「数据/路由由死锁层攥死」的设计，不会引入幻觉。
        """
        routes = [{"path": p, "source": s} for (p, s) in self.served_routes]
        tables: list = []
        for (p, _s) in self.served_routes:
            # 先精确匹配，再前缀匹配（如 /advertisers/123 命中 /advertisers/）
            for t in (PATH_TABLES.get(p) or PATH_TABLES.get(next((k for k in PATH_TABLES if p.startswith(k) and k.endswith("/")), None)) or []):
                if t not in tables:
                    tables.append(t)
        entities: dict = {}
        if params:
            for _k in ("campaign_id", "advertiser_id", "publisher_id",
                       "package_name", "owner_user_id"):
                if params.get(_k) is not None:
                    entities[_k] = params[_k]
        return {
            "routes": routes,
            "tables": tables,
            "entities": entities,
            "route_source": route_source,
            "date_or_month": date_or_month,
        }

    # ----- 定时诊断（cron）支持：补齐 TeensingDataConnector 已有的 fetch_* 接口 -----
    # 这三个方法是 /tess/cron/run 调 run_scheduled_diagnosis 时直接调用的（原先只 Mock/
    # Teensing/Empty 连接器实现了，MysqlDataConnector 漏了 → cron 在 mysql 模式下 AttributeError）。
    # 全部复用 mysql 直连已有的取数方法，不引入新 SQL 风险面。

    def fetch_recent_anomalies(self, limit: int = 20, token=None) -> list:
        """定时诊断第一轮：异常预警 + 涨跌榜归并（对齐 TeensingDataConnector.fetch_recent_anomalies）。

        复用 mysql 直连已有的 _anomaly_warning（ad_traffic_warning_log / publisher_warning）
        与 _overview_fluctuation（report_month 月度环比 rising/falling），按 campaign_id 归并，
        构造可供 normalize_to_context 消费的原始事件；fluctuation 补齐 revenue/change/_direction。
        """
        warn = self._anomaly_warning({})
        fluc = self._overview_fluctuation({})

        # 涨跌榜按 campaign_id 建索引（与 Teensing 版一致）
        fluc_map: dict = {}
        if isinstance(fluc, dict):
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if not isinstance(it, dict):
                        continue
                    cid = it.get("campaign_id")
                    if cid is None:
                        continue
                    merged = dict(it)
                    merged["_direction"] = grp
                    fluc_map[str(cid)] = merged

        raws: list = []
        for it in warn or []:
            if not isinstance(it, dict):
                continue
            cid = it.get("campaign_id")
            merged = dict(it)
            key = str(cid) if cid is not None else None
            if key is not None and key in fluc_map:
                # fluctuation 的环比变化/方向/营收补齐到预警项上
                for k, v in fluc_map[key].items():
                    merged.setdefault(k, v)
                merged["_direction"] = fluc_map[key].get("_direction", "unknown")
            else:
                merged["_direction"] = merged.get("_direction", "unknown")
            raws.append(merged)

        # 若 anomaly-warning 为空，则直接用 fluctuation 列表（含完整环比信号）
        if not raws and isinstance(fluc, dict):
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if isinstance(it, dict):
                        merged = dict(it)
                        merged["_direction"] = grp
                        raws.append(merged)

        return raws[:limit]

    def fetch_realtime_kpi(self, token=None) -> dict:
        """定时诊断第二轮：实时 KPI 小时级曲线（对齐 TeensingDataConnector.fetch_realtime_kpi）。

        从 report_hour_new 取「今天 / 昨天」逐小时聚合，构造与 adminv3 realtime-kpi 同构的
        {items:[{hour, today_revenue, today_clicks, today_conversions,
                 yesterday_revenue, yesterday_clicks, yesterday_conversions}]}，
        供 extract_realtime_anomalies 做掉零/暴跌判定。
        说明：平台级全 campaign 两日聚合窗口可控；查询失败返回空 items（不误报、不崩）。
        """
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            rows = self._query(
                "SELECT hour, "
                "SUM(CASE WHEN date=%s THEN revenue END) t_rev, "
                "SUM(CASE WHEN date=%s THEN clicks END) t_clk, "
                "SUM(CASE WHEN date=%s THEN conversions END) t_conv, "
                "SUM(CASE WHEN date=%s THEN revenue END) y_rev, "
                "SUM(CASE WHEN date=%s THEN clicks END) y_clk, "
                "SUM(CASE WHEN date=%s THEN conversions END) y_conv "
                "FROM report_hour_new WHERE date IN (%s, %s) "
                "GROUP BY hour ORDER BY hour",
                (today, today, today, yest, yest, yest, today, yest),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch_realtime_kpi 查询失败: %s", e)
            return {"items": []}
        items = []
        for r in rows:
            # report_hour_new.hour 可能是 int 或 'HH' 字符串，统一成两位字符串
            h = r.get("hour")
            hstr = f"{int(h):02d}" if h is not None else "00"
            items.append({
                "hour": hstr,
                "today_revenue": self._f(r.get("t_rev")),
                "today_clicks": int(self._f(r.get("t_clk"))),
                "today_conversions": int(self._f(r.get("t_conv"))),
                "yesterday_revenue": self._f(r.get("y_rev")),
                "yesterday_clicks": int(self._f(r.get("y_clk"))),
                "yesterday_conversions": int(self._f(r.get("y_conv"))),
            })
        return {"items": items}

    def fetch_campaign_time_series(self, campaign_id, token=None, days=7,
                                   granularity="day", publisher_id=None) -> dict:
        """定时诊断用：单 campaign 历史趋势（对齐 TeensingDataConnector.fetch_campaign_time_series）。

        复用 _campaign_kpi_trend（report_hour_new 日级明细，_recent_window 已排除今天），
        重塑为 {campaign_id, granularity, range, data_points_count, time_series:[...]}。
        hour 粒度在 report_hour_new 直接按 date,hour 聚合（窗口收窄到 3 天避免大扫表）。
        """
        try:
            cid = str(campaign_id)
            if granularity == "hour":
                ds, de = self._recent_window(min(days, 3))
                rows = self._query(
                    "SELECT date, hour, SUM(revenue) rev, SUM(payout) pay, "
                    "SUM(clicks) clk, SUM(conversions) conv FROM report_hour_new "
                    "WHERE campaign_id=%s AND date BETWEEN %s AND %s "
                    "GROUP BY date, hour ORDER BY date, hour",
                    (cid, ds, de),
                )
                series = []
                for r in rows:
                    h = r.get("hour")
                    ts = f"{r.get('date')} {int(h):02d}:00" if h is not None else r.get("date")
                    rev = self._f(r.get("rev")); pay = self._f(r.get("pay"))
                    clk = int(self._f(r.get("clk"))); conv = int(self._f(r.get("conv")))
                    series.append({
                        "timestamp": ts,
                        "revenue": round(rev, 2),
                        "profit": round(rev - pay, 2),
                        "cvr_percent": round(conv / clk * 100, 2) if clk > 0 else 0.0,
                        "margin_percent": round((rev - pay) / rev * 100, 2) if rev > 0 else 0.0,
                        "clicks": clk,
                        "conversions": conv,
                    })
            else:
                kt = self._campaign_kpi_trend({"campaign_ids": cid})
                series = []
                for it in kt.get("items") or []:
                    rev = self._f(it.get("revenue")); pay = self._f(it.get("payout"))
                    clk = int(self._f(it.get("clicks"))); conv = int(self._f(it.get("conversions")))
                    series.append({
                        "timestamp": it.get("time_label"),
                        "revenue": round(rev, 2),
                        "profit": round(rev - pay, 2),
                        "cvr_percent": it.get("cvr", 0.0),
                        "margin_percent": it.get("margin_rate", 0.0),
                        "clicks": clk,
                        "conversions": conv,
                    })
            ds, de = self._recent_window(days if granularity != "hour" else min(days, 3))
            return {
                "campaign_id": cid,
                "granularity": granularity,
                "range": f"{ds} to {de}",
                "data_points_count": len(series),
                "time_series": series,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch_campaign_time_series 失败 campaign=%s: %s", campaign_id, e)
            return {"campaign_id": str(campaign_id), "granularity": granularity, "time_series": []}

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

    def _report_month(self, params: dict) -> dict:
        """模拟 GET /report/month：按月 + advertiser 聚合，返回 items + total_data。

        report_month 原始行数巨大（如 202609 Melodong 有 2 万+ 行），直接全量返回会撑爆
        prompt。这里按 advertiser_id 分组（通常 <100 行），既保证月度总计精确，又控制体积。
        月度趋势由上层 finance_check 对 items 再按 month 汇总得到；total_data 为区间精确合计。
        """
        sd_raw = params.get("start_date") or params.get("end_date")
        ed_raw = params.get("end_date") or sd_raw
        if not sd_raw:
            sd = ed = self._current_ym()
        else:
            try:
                sd, ed = int(str(sd_raw)), int(str(ed_raw))
            except (ValueError, TypeError):
                return {"items": [], "total_data": {}}

        where = ["date BETWEEN %s AND %s"]
        args: list = [sd, ed]

        aid = params.get("advertiser_id") or params.get("advertiser_ids")
        if aid:
            aids = [x for x in str(aid).split(",") if x.strip().isdigit()]
            if aids:
                where.append(f"advertiser_id IN ({','.join(['%s'] * len(aids))})")
                args.extend(int(x) for x in aids)
        cid = params.get("campaign_id") or params.get("campaign_ids")
        if cid:
            cids = [x for x in str(cid).split(",") if x.strip().isdigit()]
            if cids:
                where.append(f"campaign_id IN ({','.join(['%s'] * len(cids))})")
                args.extend(int(x) for x in cids)

        base_where = " AND ".join(where)
        try:
            items_sql = (
                "SELECT date AS month, rm.advertiser_id, COALESCE(adv.name, '') AS advertiser_name, "
                "SUM(revenue) AS revenue, SUM(scrub_revenue) AS scrub_revenue, "
                "SUM(revenue - scrub_revenue) AS calc_revenue, "
                "SUM(payout) AS payout, SUM(scrub_payout) AS scrub_payout, "
                "SUM(payout - scrub_payout) AS calc_payout, "
                "SUM(conversions) AS conversions, SUM(scrub_conversions) AS scrub_conversions "
                "FROM report_month rm LEFT JOIN advertiser adv ON adv.id = rm.advertiser_id "
                f"WHERE {base_where} "
                "GROUP BY date, rm.advertiser_id ORDER BY date DESC, revenue DESC"
            )
            rows = self._query(items_sql, tuple(args))
            items = [
                {
                    "month": str(r.get("month") or ""),
                    "date": str(r.get("month") or ""),
                    "advertiser_id": r.get("advertiser_id"),
                    "advertiser_name": r.get("advertiser_name") or "",
                    "revenue": round(self._f(r.get("revenue")), 4),
                    "scrub_revenue": round(self._f(r.get("scrub_revenue")), 4),
                    "calc_revenue": round(self._f(r.get("calc_revenue")), 4),
                    "payout": round(self._f(r.get("payout")), 4),
                    "scrub_payout": round(self._f(r.get("scrub_payout")), 4),
                    "calc_payout": round(self._f(r.get("calc_payout")), 4),
                    "conversions": int(self._f(r.get("conversions"))),
                    "scrub_conversions": int(self._f(r.get("scrub_conversions"))),
                }
                for r in rows
            ]

            total_sql = (
                "SELECT SUM(revenue) AS revenue, SUM(scrub_revenue) AS scrub_revenue, "
                "SUM(revenue - scrub_revenue) AS calc_revenue, "
                "SUM(payout) AS payout, SUM(scrub_payout) AS scrub_payout, "
                "SUM(payout - scrub_payout) AS calc_payout, "
                "SUM(conversions) AS conversions, SUM(scrub_conversions) AS scrub_conversions "
                f"FROM report_month WHERE {base_where}"
            )
            total_rows = self._query(total_sql, tuple(args))
            tr = total_rows[0] if total_rows else {}
            total_data = {
                "revenue": round(self._f(tr.get("revenue")), 4),
                "scrub_revenue": round(self._f(tr.get("scrub_revenue")), 4),
                "calc_revenue": round(self._f(tr.get("calc_revenue")), 4),
                "payout": round(self._f(tr.get("payout")), 4),
                "scrub_payout": round(self._f(tr.get("scrub_payout")), 4),
                "calc_payout": round(self._f(tr.get("calc_payout")), 4),
                "conversions": int(self._f(tr.get("conversions"))),
                "scrub_conversions": int(self._f(tr.get("scrub_conversions"))),
            }
            return {"items": items, "total_data": total_data}
        except Exception as e:
            logger.exception("_report_month failed: %s", e)
            return {"items": [], "total_data": {}}

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
                    "profit": round(rev - pay, 4),
                    "clicks": int(clk),
                    "conversions": int(conv),
                    "cr": round(cr, 4),
                    "margin": round(margin, 4),
                })
            self._agg_cache[key] = all_rows
        start = (page - 1) * page_size
        return {"items": all_rows[start: start + page_size]}

    def _report_daily_campaign_change(self, anchor: str) -> dict:
        """日环比涨跌榜：report_hour_new 按 (date, campaign) 聚合，对比最近两天（anchor 当天 vs 前一天）
        的逐 Campaign 利润/营收，计算日环比(profit_change / revenue_change)。
        供「今日/昨天 哪个 Campaign 利润环比下滑最快」这类『日级』问法——与 fluctuation 的月度环比区分。

        anchor 形如 'YYYY-MM-DD'（通常 today 或 yesterday）。
        返回 {items:[{campaign_id, campaign_name, revenue, profit, revenue_change, profit_change,
                      revenue_change_pct, profit_change_pct, margin}], date_curr, date_prev}。
        """
        try:
            a = datetime.strptime(str(anchor), "%Y-%m-%d")
            d0 = (a - timedelta(days=3)).strftime("%Y-%m-%d")  # 取近 4 天，挑最近两个『有数据』的日期对比
            rows = self._query_across(
                "SELECT campaign_id, date, SUM(revenue) rev, SUM(payout) pay "
                "FROM report_hour_new WHERE date >= %s GROUP BY campaign_id, date",
                (d0,),
            )
            by_c: dict = {}
            dates_present: set = set()
            for r in rows:
                cid = str(r.get("campaign_id"))
                d = str(r.get("date"))
                dates_present.add(d)
                rev = self._f(r.get("rev"))
                pay = self._f(r.get("pay"))
                by_c.setdefault(cid, {})[d] = (rev, pay)
            if len(dates_present) < 2:
                return {"items": [], "date_curr": anchor, "date_prev": None}
            sd = sorted(dates_present)
            d_curr, d_prev = sd[-1], sd[-2]
            out = []
            for cid, m in by_c.items():
                if d_curr not in m or d_prev not in m:
                    continue  # 两日都要有数才算『环比』
                rev_c, pay_c = m[d_curr]
                rev_p, pay_p = m[d_prev]
                profit_c = rev_c - pay_c
                profit_p = rev_p - pay_p
                rc = rev_c - rev_p
                pc = profit_c - profit_p
                item = {
                    "campaign_id": int(cid) if cid.isdigit() else cid,
                    "revenue": round(rev_c, 2),
                    "profit": round(profit_c, 2),
                    "revenue_change": round(rc, 2),
                    "profit_change": round(pc, 2),
                }
                item["revenue_change_pct"] = round(rc / rev_p * 100, 2) if rev_p else (100.0 if rev_c else 0.0)
                item["profit_change_pct"] = round(pc / profit_p * 100, 2) if profit_p else (100.0 if profit_c else 0.0)
                item["margin"] = round(profit_c / rev_c * 100, 2) if rev_c > 0 else 0.0
                out.append(item)
            names = self._campaign_meta([str(i["campaign_id"]) for i in out])
            for i in out:
                i["campaign_name"] = (names.get(str(i["campaign_id"])) or {}).get("name") or ""
            return {"items": out, "date_curr": d_curr, "date_prev": d_prev}
        except Exception as e:
            logger.exception("_report_daily_campaign_change failed: %s", e)
            return {"items": [], "date_curr": anchor, "date_prev": None}

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

    def _channel_quality(self, params: dict) -> dict:
        """模拟 GET /campaign-quality/publisher/channels（渠道级质量，publisher_id + campaign_ids）。

        从 report_hour_new 按 channel_id 聚合近 7 天质量时序；publisher_id 为 int、campaign_id
        为 VARCHAR(64) 必须按字符串传（否则索引失效）。channel_id 列若不存在则查询抛错，
        由上游 _safe_api_get 捕获降级为空（绝不回退 API，符合「全部落到 MySQL 直连」）。
        """
        pid = params.get("publisher_id")
        cids = [x for x in str(params.get("campaign_ids") or "").split(",") if x]
        if pid is None or not cids:
            return {"items": []}
        ds, de = self._recent_window(7)
        ph = ",".join(["%s"] * len(cids))
        sql = (
            "SELECT channel_id, date, SUM(conversions) conv, SUM(postback_conversions) pb, "
            "SUM(clicks) clk, SUM(reject_count) rj FROM report_hour_new "
            f"WHERE publisher_id=%s AND campaign_id IN ({ph}) AND date BETWEEN %s AND %s "
            "GROUP BY channel_id, date"
        )
        rows = self._query(sql, (int(pid), *cids, ds, de))
        by_ch: dict = {}
        for r in rows:
            ch = r.get("channel_id")
            by_ch.setdefault(ch, {})[r["date"]] = {
                "conversions": self._f(r.get("conv")),
                "postback_conversions": self._f(r.get("pb")),
                "clicks": int(self._f(r.get("clk"))),
                "reject_count": int(self._f(r.get("rj"))),
            }
        items = []
        for ch, dates in by_ch.items():
            per_date = {}
            tc = tp = tk = tr = 0.0
            for d, a in dates.items():
                per_date[d] = self._quality_item(d, a["conversions"], a["postback_conversions"], a["clicks"], a["reject_count"], 0, 0)
                tc += a["conversions"]; tp += a["postback_conversions"]; tk += a["clicks"]; tr += a["reject_count"]
            total = self._quality_total(tc, tp, tk, tr, 0, 0)
            items.append({
                "channel_id": ch,
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
        # ETIT：event_postback_info（install_time → create_time），两平台都有，独立容错
        try:
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
        except Exception as e:  # noqa: BLE001
            logger.warning("MysqlDataConnector ETIT 失败(campaign=%s)：%s", cid, e)
        # CTIT：install_time − click_time（需 click 表；seanear 无 click 表 → 静默留空，不崩溃）
        try:
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
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "MysqlDataConnector CTIT 失败(campaign=%s，可能无 click 表)：%s", cid, e)
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

    # ----- 安全回退 helper（新端点通用） -----

    def _mysql_only(self, path, fn, params, token=None, timeout=None):
        """只走 MySQL 实现；任何异常直接抛出，由上游 _safe_api_get 捕获并标记 error。

        绝不回退真实 API —— 保证「全部落到 MySQL 直连」：MySQL 取数失败/缺表时宁愿让上游
        把该字段标 error（Gatekeeper 熔断 INCONCLUSIVE），也绝不静默返回部分/截断的 API 数据
        （正是此前「数据不全」反复出现的根因）。
        """
        self.served_routes.append((path, "mysql"))
        return fn(params)

    # ----- 批量映射辅助（ranking/fluctuation 补主数据） -----

    def _advertiser_profile_map(self, aids: list) -> dict:
        """批量取 advertiser 档案，按 id 索引（int/str 兼容）。"""
        aids = [a for a in aids if a is not None]
        if not aids:
            return {}
        try:
            ids = tuple(int(a) for a in aids)
        except (TypeError, ValueError):
            ids = tuple(aids)
        ph = ",".join(["%s"] * len(ids))
        rows = self._query(
            f"SELECT id, name, am, bd, status FROM advertiser WHERE id IN ({ph})", ids)
        return {
            r["id"]: {"name": r.get("name"), "am": r.get("am"),
                      "bd": r.get("bd"), "status": r.get("status")}
            for r in rows
        }

    def _campaign_adv_map(self, cids: list) -> dict:
        """批量取 campaign_id -> advertiser_id，按 str(campaign_id) 索引。"""
        cids = [c for c in cids if c]
        if not cids:
            return {}
        try:
            ids = tuple(int(c) for c in cids)
        except (TypeError, ValueError):
            ids = tuple(cids)
        ph = ",".join(["%s"] * len(ids))
        rows = self._query(
            f"SELECT id, advertiser_id FROM campaign WHERE id IN ({ph})", ids)
        return {str(r["id"]): r.get("advertiser_id") for r in rows}

    # ----- 平台级榜单 / 主数据实现（dashboard 类，预聚合表，秒级） -----

    def _latest_closed_month(self) -> int:
        """report_month 不含当前进行月；取最大的已结月作为『当前榜单月』。"""
        rows = self._query("SELECT MAX(date) m FROM report_month")
        m = rows[0]["m"] if rows and rows[0]["m"] is not None else None
        if m is None:
            cur = self._current_ym()
            y, mo = divmod(cur - 1, 100)
            if mo == 0:
                y -= 1
                mo = 12
            m = y * 100 + mo
        return int(m)

    @staticmethod
    def _prev_month(m: int) -> int:
        y, mo = divmod(m, 100)
        mo -= 1
        if mo == 0:
            mo, y = 12, y - 1
        return y * 100 + mo

    def _overview_ranking(self, params: dict) -> list:
        """模拟 GET /overview/ranking：按『最近已结月』营收排名的广告主榜。

        用 report_month（月度预聚合，秒级）GROUP BY advertiser_id；JOIN advertiser
        补 name/am/bd/status；revenue_change 取与上一已结月的环比。返回 list。

        形状对齐 adminv3 消费侧（account_overview 取 advertiser_id/advertiser_name/
        revenue/profit/margin/revenue_change）。

        取舍说明：report_month 不含当前进行月，榜单反映最近已结月（非滚动 30 天）。
        实时滚动榜需扫 report_hour_new 4900 万行超取数预算，故用预聚合月榜。
        """
        m = self._latest_closed_month()
        pm = self._prev_month(m)
        cur = self._query(
            "SELECT advertiser_id, SUM(revenue) rev, SUM(payout) pay, "
            "SUM(conversions) conv FROM report_month "
            "WHERE date=%s GROUP BY advertiser_id", (m,))
        prev = self._query(
            "SELECT advertiser_id, SUM(revenue) rev FROM report_month "
            "WHERE date=%s GROUP BY advertiser_id", (pm,))
        prev_map = {str(r["advertiser_id"]): self._f(r["rev"]) for r in prev}
        aids = [r["advertiser_id"] for r in cur if r["advertiser_id"] is not None]
        meta = self._advertiser_profile_map(aids)
        out = []
        for r in cur:
            aid = r["advertiser_id"]
            rev = self._f(r["rev"])
            pay = self._f(r["pay"])
            conv = int(self._f(r["conv"]))
            margin = ((rev - pay) / rev * 100) if rev > 0 else 0.0
            prev_rev = prev_map.get(str(aid), 0.0)
            chg = ((rev - prev_rev) / prev_rev * 100) if prev_rev > 0 else (100.0 if rev > 0 else 0.0)
            m0 = meta.get(aid, {})
            out.append({
                "advertiser_id": aid,
                "advertiser_name": m0.get("name"),
                "revenue": round(rev, 4),
                "payout": round(pay, 4),
                "profit": round(rev - pay, 4),
                "conversions": conv,
                "margin": round(margin, 4),
                "revenue_change": round(chg, 2),
                "status": m0.get("status"),
                "status_name": campaign_status_label(m0.get("status")),
                "am": m0.get("am"),
                "bd": m0.get("bd"),
            })
        out.sort(key=lambda x: x["revenue"], reverse=True)
        return out[:100]

    def _overview_fluctuation(self, params: dict) -> dict:
        """模拟 GET /overview/ranking/fluctuation：最近两已结月 campaign 级环比涨跌榜。

        返回 {rising:[...], falling:[...]}；每个 item 含 campaign_id/campaign_name/
        advertiser_id/revenue/profit/margin/revenue_change/prev_revenue 等，供
        campaign_ranking（按 revenue_change 排序）与 daily_summary 消费。

        取舍：用 report_month 月度环比（非 7 天环比），因 report_hour_new 平台级
        实时聚合超预算；月榜反映最近两已结月变化，已足够识别大幅涨跌。
        """
        m = self._latest_closed_month()
        pm = self._prev_month(m)
        cur = self._query(
            "SELECT campaign_id, SUM(revenue) rev, SUM(payout) pay, "
            "SUM(conversions) conv FROM report_month "
            "WHERE date=%s GROUP BY campaign_id", (m,))
        prev = self._query(
            "SELECT campaign_id, SUM(revenue) rev, SUM(payout) pay FROM report_month "
            "WHERE date=%s GROUP BY campaign_id", (pm,))
        prev_map = {str(r["campaign_id"]): {"rev": self._f(r["rev"]), "pay": self._f(r["pay"])} for r in prev}
        items = []
        for r in cur:
            cid = str(r["campaign_id"])
            rev = self._f(r["rev"])
            pay = self._f(r["pay"])
            profit = rev - pay
            conv = int(self._f(r["conv"]))
            margin = (profit / rev * 100) if rev > 0 else 0.0
            prev = prev_map.get(cid, {})
            prev_rev = prev.get("rev", 0.0)
            prev_pay = prev.get("pay", 0.0)
            prev_profit = prev_rev - prev_pay
            chg = ((rev - prev_rev) / prev_rev * 100) if prev_rev > 0 else (100.0 if rev > 0 else 0.0)
            profit_chg = ((profit - prev_profit) / prev_profit * 100) if prev_profit > 0 else (100.0 if profit > 0 else 0.0)
            items.append({
                "campaign_id": int(cid) if cid.isdigit() else cid,
                "revenue": round(rev, 4),
                "payout": round(pay, 4),
                "profit": round(profit, 4),
                "conversions": conv,
                "margin": round(margin, 4),
                "revenue_change": round(chg, 2),
                "profit_change": round(profit_chg, 2),
                "prev_revenue": round(prev_rev, 4),
            })
        rising = sorted(
            [i for i in items if i["revenue_change"] > 0],
            key=lambda x: x["revenue_change"], reverse=True)[:50]
        falling = sorted(
            [i for i in items if i["revenue_change"] < 0],
            key=lambda x: x["revenue_change"])[:50]
        # 仅给 top 100 campaign 补 name/advertiser_id（避免对上万 id 做 IN 大查询，稳健且快）
        top_cids = [str(i["campaign_id"]) for i in (rising + falling)]
        names = self._campaign_meta(top_cids)
        amap = self._campaign_adv_map(top_cids)
        for i in rising + falling:
            cid = str(i["campaign_id"])
            i["campaign_name"] = names.get(cid, {}).get("name")
            i["advertiser_id"] = amap.get(cid)
        return {"rising": rising[:50], "falling": falling[:50]}

    def _daily_kpi(self, params: dict) -> list:
        """模拟 GET /overview/daily-kpi：平台级日 KPI 时间序列（report_day 预聚合，秒级）。

        返回按 date 升序的 list：[{date, revenue, payout, profit, clicks, conversions,
        postback_conversions, reject_count}]，消费侧取末两位做环比（_pct_delta 读
        revenue/profit/payout/clicks/conversions）。默认窗口 30 天、结束于请求 date
        （缺省=昨天），避免把『今天』部分日当完整日。
        """
        end = params.get("date") or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        days = int(params.get("days") or 30)
        try:
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            start = end
        rows = self._query(
            "SELECT date, SUM(revenue) rev, SUM(payout) pay, SUM(clicks) clk, "
            "SUM(conversions) conv, SUM(postback_conversions) pb, SUM(reject_count) rj "
            "FROM report_day WHERE date BETWEEN %s AND %s GROUP BY date ORDER BY date",
            (start, end))
        out = []
        for r in rows:
            rev = self._f(r.get("rev"))
            pay = self._f(r.get("pay"))
            clk = int(self._f(r.get("clk")))
            conv = int(self._f(r.get("conv")))
            out.append({
                "date": r.get("date"),
                "revenue": round(rev, 4),
                "payout": round(pay, 4),
                "profit": round(rev - pay, 4),
                "clicks": clk,
                "conversions": conv,
                "postback_conversions": int(self._f(r.get("pb"))),
                "reject_count": int(self._f(r.get("rj"))),
            })
        return out

    def _advertisers_list(self, params: dict) -> dict:
        """模拟 GET /advertisers：广告主目录（支持 am/bd/status 过滤 + 分页 + 关键字）。"""
        page = int(params.get("page") or 1)
        page_size = min(int(params.get("page_size") or 50), 200)
        am = params.get("am")
        bd = params.get("bd")
        status = params.get("status")
        q = params.get("q") or params.get("search")
        where = []
        wargs = []
        if am not in (None, ""):
            where.append("am=%s")
            wargs.append(am)
        if bd not in (None, ""):
            where.append("bd=%s")
            wargs.append(bd)
        if status not in (None, ""):
            where.append("status=%s")
            wargs.append(status)
        if q:
            where.append("(name LIKE %s OR user_name LIKE %s OR id LIKE %s)")
            like = f"%{q}%"
            wargs += [like, like, like]
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total_rows = self._query(f"SELECT COUNT(*) c FROM advertiser{wsql}", wargs)
        total = total_rows[0]["c"] if total_rows else 0
        offset = (page - 1) * page_size
        rows = self._query(
            f"SELECT id, name, user_name, bd, am, status, margin, jointime, company_name "
            f"FROM advertiser{wsql} ORDER BY id DESC LIMIT %s OFFSET %s",
            tuple(wargs) + (page_size, offset))
        items = [{
            "id": r.get("id"),
            "name": r.get("name"),
            "user_name": r.get("user_name"),
            "bd": r.get("bd"),
            "am": r.get("am"),
            "status": r.get("status"),
            "status_name": campaign_status_label(r.get("status")),
            "margin": self._f(r.get("margin")),
            "jointime": str(r.get("jointime")) if r.get("jointime") is not None else None,
            "company_name": r.get("company_name"),
        } for r in rows]
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    def _global_settings(self, params: dict) -> dict:
        """模拟 GET /global-settings：从 global_setting 表读 key/value；暴露 global_cap。"""
        try:
            rows = self._query("SELECT key_name, key_value FROM global_setting")
        except Exception:  # noqa: BLE001 —— 表不存在等，安全返回 None
            return {"global_cap": None}
        kv = {r["key_name"]: r["key_value"] for r in rows}
        cap = kv.get("global_cap") or kv.get("cap") or kv.get("global_cap_limit")
        try:
            cap = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        out = {"global_cap": cap}
        out.update(kv)
        return out

    # ----- 配置 / 映射 / 规则（traffic_policy / 实体解析 / 异常告警）直连实现 -----
    # 这些端点原走 Teensing API，本质都是网盟 MySQL 里的配置/映射表，可直接直出，
    # 全部用 _mysql_only 包裹：只走 MySQL 直出，异常直接抛出（上游 _safe_api_get 捕获转
    # error，Gatekeeper 熔断 INCONCLUSIVE），绝不回退 API —— 保证「全部落到 MySQL 直连」。

    @staticmethod
    def _row_to_dict(r: dict) -> dict:
        """把一行（DictCursor）转成可 JSON 序列化 dict：bytes->utf8、datetime/date->iso、Decimal->float。"""
        out = {}
        for k, v in r.items():
            if isinstance(v, (bytes, bytearray)):
                try:
                    v = v.decode("utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    v = str(v)
            elif hasattr(v, "isoformat"):  # datetime / date / time
                v = v.isoformat()
            else:
                try:
                    import decimal
                    if isinstance(v, decimal.Decimal):
                        v = float(v)
                except Exception:  # noqa: BLE001
                    pass
            out[k] = v
        return out

    @staticmethod
    def _page_limit(params: dict, default: int = 100, cap: int = 500) -> int:
        """从 params 取 page_size，夹在 [1, cap]。"""
        try:
            n = int(params.get("page_size") or default)
        except (TypeError, ValueError):
            n = default
        return max(1, min(n, cap))

    def _adv_pub_pkg_maps(self, params: dict) -> dict:
        """模拟 GET /advertiser-publisher-pkg-maps：按包名归因广告主/渠道。"""
        pkg = (params.get("packagename") or params.get("package_name") or "").strip()
        if not pkg:
            return {"items": [], "total": 0}
        try:
            rows = self._query(
                "SELECT id, publisher_id, advertiser_id, platform, packagename, "
                "appname, channel, comment, operator, status, update_time "
                "FROM advertiser_publisher_pkg_map "
                "WHERE packagename = %s OR appname = %s "
                "ORDER BY id DESC LIMIT 500",
                (pkg, pkg),
            )
        except Exception:  # noqa: BLE001
            return {"items": [], "total": 0}
        items = [self._row_to_dict(r) for r in rows]
        return {"items": items, "total": len(items)}

    def _mapping_publisher_channels(self, params: dict) -> dict:
        """模拟 GET /mapping-publisher-channels：publisher<->channel 映射。"""
        pid = params.get("publisher_id")
        ch = params.get("channel")
        conds, args = [], []
        if pid:
            pid_v = int(pid) if str(pid).isdigit() else pid
            conds.append("publisher_id = %s")
            args.append(pid_v)
        if ch:
            conds.append("channel = %s")
            args.append(ch)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        limit = self._page_limit(params, default=100, cap=500)
        try:
            rows = self._query(
                "SELECT id, publisher_id, channel, campaign_id, replace_publisher_id, "
                "replace_channel, `desc`, status, operator, update_time, advertiser_id "
                "FROM mapping_publisher_channel" + where +
                " ORDER BY id DESC LIMIT " + str(limit),
                tuple(args),
            )
        except Exception:  # noqa: BLE001
            return {"items": [], "total": 0}
        items = [self._row_to_dict(r) for r in rows]
        return {"items": items, "total": len(items)}

    def _publisher_campaign_blocks(self, params: dict) -> dict:
        """模拟 GET /publisher-campaign-blocks：屏蔽规则（普通 + 系统级）。"""
        cid = params.get("campaign_id")
        pid = params.get("publisher_id")
        conds, args = [], []
        if cid:
            conds.append("campaign_id = %s")
            args.append(int(cid) if str(cid).isdigit() else cid)
        if pid:
            conds.append("publisher_id = %s")
            args.append(int(pid) if str(pid).isdigit() else pid)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        try:
            rows_a = self._query(
                "SELECT id, publisher_id, campaign_id, `desc`, status, channels, "
                "'manual' AS source FROM publisher_campaign_block" + where,
                tuple(args),
            )
            rows_b = self._query(
                "SELECT id, publisher_id, campaign_id, reason AS `desc`, status, "
                "'' AS channels, 'system' AS source FROM system_publisher_campaign_block" + where,
                tuple(args),
            )
        except Exception:  # noqa: BLE001
            return {"items": []}
        items = [self._row_to_dict(r) for r in (list(rows_a) + list(rows_b))]
        return {"items": items, "total": len(items)}

    def _replace_channels(self, params: dict) -> dict:
        """模拟 GET /replace-channels：替换渠道规则。publisher_id 在表中可能是逗号分隔多值。"""
        pid = params.get("publisher_id")
        conds, args = [], []
        if pid:
            # 兼容 publisher_id 为逗号分隔串的情况
            conds.append("(publisher_id = %s OR FIND_IN_SET(%s, publisher_id))")
            args.append(pid)
            args.append(pid)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        try:
            rows = self._query(
                "SELECT id, advertiser_id, campaign_id, publisher_id, channel, "
                "replace_publisher_id, replace_channel, replace_channel_count, click_count, "
                "event_name, event_count, `desc`, operator, status, update_time "
                "FROM replace_channel" + where + " ORDER BY id DESC LIMIT 500",
                tuple(args),
            )
        except Exception:  # noqa: BLE001
            return {"items": []}
        items = [self._row_to_dict(r) for r in rows]
        return {"items": items, "total": len(items)}

    def _users_options(self, params: dict) -> list:
        """模拟 GET /users/options：扁平用户目录（owner 名->id 解析用）。返回 list。

        user 表无 real_name 列，以 name 兜底；analytics 同时读 name/real_name，故二者同值。
        """
        try:
            rows = self._query(
                "SELECT id, name, status, role, email FROM `user` ORDER BY id LIMIT 500")
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in rows:
            out.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "real_name": r.get("name"),
                "status": r.get("status"),
                "role": r.get("role"),
            })
        return out

    def _anomaly_warning(self, params: dict) -> list:
        """模拟 GET /overview/ranking/anomaly-warning：异常告警列表。返回 list。"""
        try:
            logs = self._query(
                "SELECT id, rule_name, rule_code, log_date, advertiser_id, advertiser_name, "
                "publisher_id, publisher_name, campaign_id, campaign_name, package_name, "
                "warning_level, real_value, desired_value, clicks, conversions, cvr, "
                "handle_status, created_at FROM ad_traffic_warning_log "
                "ORDER BY created_at DESC LIMIT 50")
        except Exception:  # noqa: BLE001
            logs = []
        items = [self._row_to_dict(r) for r in logs]
        # 补充 publisher_warning 阈值规则（若存在）
        try:
            pw = self._query(
                "SELECT id, publisher_id, campaign_id, min_cvr, max_cvr, status, create_time "
                "FROM publisher_warning LIMIT 100")
            for r in pw:
                items.append(self._row_to_dict(r))
        except Exception:  # noqa: BLE001
            pass
        return items

    def _campaign_detail(self, params: dict) -> dict:
        """模拟 GET /campaign-detail：组合 campaign/advertiser 配置直出真实数据。

        原 API 的 campaigns/events/advertisers 为嵌套结构；此处用实体表直出，字段无法与 API
        100% 对齐时由下游 Gatekeeper 容错。包裹在 _mysql_only 中，任何异常回退 API。
        """
        cid = params.get("campaign_ids") or params.get("campaign_id")
        if cid and "," in str(cid):
            cid = str(cid).split(",")[0]
        if not cid:
            return {"t_campaign_id": None, "campaigns": [], "events": [], "advertisers": []}
        try:
            cid_i = int(cid)
        except (TypeError, ValueError):
            return {"t_campaign_id": cid, "campaigns": [], "events": [], "advertisers": []}
        try:
            camps = self._query(
                "SELECT * FROM campaign WHERE id = %s LIMIT 1", (cid_i,))
            adv_id = camps[0].get("advertiser_id") if camps else None
            advs = self._query(
                "SELECT * FROM advertiser WHERE id = %s LIMIT 1", (adv_id,)) if adv_id else []
        except Exception:  # noqa: BLE001
            camps, advs = [], []
        return {
            "t_campaign_id": cid,
            "campaigns": [self._row_to_dict(c) for c in camps],
            "events": [],  # 事件明细暂无独立实体表，留空（下游容错）
            "advertisers": [self._row_to_dict(a) for a in advs],
        }

    def _campaign_compare(self, params: dict) -> dict:
        """模拟 GET /campaign-compare：某 campaign 当前周期 vs 上一等长周期对比。

        数据源 report_hour_new（网盟库里唯一带 campaign_id 的明细表）；
        campaign_id 必须按字符串传以走索引（否则全表扫 4900 万行）。
        profit = revenue - payout；cvr = conversions / clicks。包裹在 _mysql_only 中容错。
        """
        import datetime as _dt
        cid_raw = params.get("campaign_ids") or params.get("campaign_id") or ""
        if "," in str(cid_raw):
            cid_raw = str(cid_raw).split(",")[0]
        cid = str(cid_raw).strip()
        if not cid:
            return {"current_period": {}, "previous_period": {},
                    "revenue": 0, "profit": 0, "conversions": 0, "cvr": 0}
        ds = params.get("date_start")
        de = params.get("date_end")
        if not ds or not de:
            de = _dt.datetime.now().strftime("%Y-%m-%d")
            ds = (_dt.datetime.now() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")

        def _agg(ads, ade):
            try:
                row = self._query(
                    "SELECT SUM(revenue) rev, SUM(payout) pay, SUM(conversions) conv, SUM(clicks) clk "
                    "FROM report_hour_new WHERE campaign_id=%s AND date BETWEEN %s AND %s",
                    (cid, ads, ade))
                r = row[0] if row else {}
            except Exception:  # noqa: BLE001
                r = {}
            rev = float(r.get("rev") or 0)
            pay = float(r.get("pay") or 0)
            conv = int(r.get("conv") or 0)
            clk = int(r.get("clk") or 0)
            return {
                "date_start": ads, "date_end": ade,
                "revenue": round(rev, 2), "payout": round(pay, 2),
                "profit": round(rev - pay, 2), "conversions": conv, "clicks": clk,
                "cvr": round(conv / clk, 4) if clk else 0,
            }

        try:
            d0 = _dt.datetime.strptime(ds, "%Y-%m-%d")
            d1 = _dt.datetime.strptime(de, "%Y-%m-%d")
            length = (d1 - d0).days + 1
            prev_de = (d0 - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
            prev_ds = (d0 - _dt.timedelta(days=length)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            prev_ds, prev_de = ds, de

        cur = _agg(ds, de)
        prev = _agg(prev_ds, prev_de)
        return {
            "current_period": cur,
            "previous_period": prev,
            "revenue": cur["revenue"], "profit": cur["profit"],
            "conversions": cur["conversions"], "cvr": cur["cvr"],
            "current_period_revenue": cur["revenue"],
            "previous_period_revenue": prev["revenue"],
        }
