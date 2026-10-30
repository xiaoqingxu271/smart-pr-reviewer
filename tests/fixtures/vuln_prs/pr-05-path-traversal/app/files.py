import os

BASE = "/var/data"


def read_user_file(filename):
    path = os.path.join(BASE, filename)
    with open(path) as f:
        return f.read()
