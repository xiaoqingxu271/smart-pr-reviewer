import sqlite3


def list_orders(tenant_id):
    conn = sqlite3.connect("orders.db")
    rows = conn.execute("SELECT id FROM orders WHERE tenant = ?", (tenant_id,)).fetchall()
    conn.close()
    return rows
