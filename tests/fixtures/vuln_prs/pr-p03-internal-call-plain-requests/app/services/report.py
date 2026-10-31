import requests


def fetch_quota(host):
    resp = requests.get(f"http://{host}:8080/quota", timeout=3)
    return resp.json()
