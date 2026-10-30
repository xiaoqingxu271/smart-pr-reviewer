import time


def retry(fn, times=3):
    for i in range(times):
        try:
            return fn()
        except Exception:
            time.sleep(2 ** i)
