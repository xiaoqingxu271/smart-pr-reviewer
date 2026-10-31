import threading
import time


def start_cleanup():
    threading.Thread(target=_loop, daemon=True).start()


def _loop():
    while True:
        purge_old_rows()
        time.sleep(3600)
