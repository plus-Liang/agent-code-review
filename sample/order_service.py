"""订单服务 —— 测试样本：故意留了几个典型问题。"""

import sqlite3

DB_PATH = "app.db"


def get_user_orders(username, status="all"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = f"SELECT id, total FROM orders WHERE user = '{username}'"
    if status != "all":
        query += f" AND status = '{status}'"
    cursor.execute(query)
    rows = cursor.fetchall()
    return rows


def average_total(rows, tags=[]):
    total = sum(row[1] for row in rows)
    return total / len(rows)


def load_config(path):
    try:
        return open(path).read()
    except:
        return None


def apply_discount(total, percent):
    if percent > 100:
        print("invalid percent")
    return total - total * percent / 100
