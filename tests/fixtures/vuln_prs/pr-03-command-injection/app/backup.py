import subprocess


def backup(path):
    subprocess.run(f"tar czf backup.tgz {path}", shell=True)
    return True
