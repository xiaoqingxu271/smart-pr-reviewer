from contextlib import contextmanager


@contextmanager
def managed():
    yield 1
