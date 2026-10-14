# 故意埋的测试代码 - 用于验证 GitHub Action
import sqlite3

def unsafe_query(user_id):
    conn = sqlite3.connect('test.db')
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE id = {user_id}"   # SQL 注入
    cursor.execute(query)
    return cursor.fetchall()

def risky_divide(a, b):
    return a / b   # 没处理除零