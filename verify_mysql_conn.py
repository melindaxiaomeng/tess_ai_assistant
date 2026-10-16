#!/usr/bin/env python3
"""Tess 直连 MySQL 最小自检（不依赖 tess_backend，纯 pymysql）。

用途：在 Tess 容器里一行跑出「为什么 MySQL 路径返回 0 campaign」，隔离三种根因：
  1) 连不上 172.22.0.6                 -> 看 reachable=False / error
  2) 数据在 seanear 库不在 melocpi     -> 看哪个库的 window_rows>0
  3) report_campaign.date 不是 YYYY-MM-DD -> 看 date_sample 格式（决定 BETWEEN 是否生效）

用法（容器内，env 已注入 TESS_MYSQL_*）：
  docker exec tess-diagnose python3 /verify_mysql_conn.py
或本地：
  TESS_MYSQL_HOST=172.22.0.6 TESS_MYSQL_USER=melo TESS_MYSQL_PASSWORD=xxx \
  TESS_MYSQL_DATABASES='{"Melodong":"melocpi","seanear":"seanear"}' \
  python3 verify_mysql_conn.py
"""
import os
import sys

AID = int(os.getenv("VERIFY_AID", "1000853"))
DS, DE = os.getenv("VERIFY_DS", "2026-07-01"), os.getenv("VERIFY_DE", "2026-10-31")
HOST = os.getenv("TESS_MYSQL_HOST", "172.22.0.6")
PORT = int(os.getenv("TESS_MYSQL_PORT", "3306"))
USER = os.getenv("TESS_MYSQL_USER", "melo")
PASSWORD = os.getenv("TESS_MYSQL_PASSWORD", "")
raw_db = os.getenv("TESS_MYSQL_DATABASES", '{"Melodong":"melocpi","seanear":"seanear"}')
try:
    DBS = __import__("json").loads(raw_db)
except Exception:
    DBS = {"Melodong": "melocpi", "seanear": "seanear"}


def q(db, sql, params):
    import pymysql
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                           database=db, charset="utf8mb4", connect_timeout=15,
                           cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def main():
    print("host=%s:%s user=%s aid=%s window=%s~%s" % (HOST, PORT, USER, AID, DS, DE))
    print("databases=%s" % DBS)
    found_in = None
    for name, db in DBS.items():
        print("\n--- [%s] db=%s ---" % (name, db))
        try:
            r = q(db, "SELECT COUNT(*) AS c FROM report_campaign WHERE advertiser_id=%s", (AID,))
            total = r[0]["c"] if r else None
            r2 = q(db, "SELECT COUNT(*) AS c FROM report_campaign WHERE advertiser_id=%s AND date BETWEEN %s AND %s", (AID, DS, DE))
            win = r2[0]["c"] if r2 else None
            r3 = q(db, "SELECT DISTINCT date FROM report_campaign WHERE advertiser_id=%s LIMIT 5", (AID,))
            dates = [x["date"] for x in r3] if r3 else []
            print("  总条数(无日期过滤)=%s  窗口内(%s~%s)=%s" % (total, DS, DE, win))
            print("  date 样本(格式)=%s" % dates)
            if win:
                found_in = (name, db)
        except Exception as e:  # noqa: BLE001
            print("  !! 不可达 / 报错: %s: %s" % (type(e).__name__, e))
    print("\n=== 结论 ===")
    if found_in:
        print("数据在库 [%s]=%s（窗口内>0）。把 MysqlDataConnector 的 platform_id 选库指向它，" % found_in)
        print("或确认 advertiser %d 实际属于 %s 平台。" % (AID, found_in[0]))
    else:
        print("两个库窗口内都为 0：检查 date 列格式（上面 date 样本）或 advertiser_id 是否正确。")


if __name__ == "__main__":
    sys.exit(main())
