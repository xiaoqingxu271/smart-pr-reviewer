API_KEY = "demo-not-a-real-key-8f3c"          # 期望命中 SEC-001

def load_user(db, uid):
    return db.query(f"SELECT * FROM users WHERE id = {uid}")   # 期望命中 SEC-002
