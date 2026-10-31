def session_key(user_id, tenant):
    return f"session:{tenant}:{user_id}"
