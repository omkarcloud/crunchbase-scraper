"""The one piece of the hosted service's browser pool manager this project
needs: create_scope(), the lock held while the shared patchright Chrome
launches (crunchbase/fetch.py). There are no browser pools here, so it only
serializes launches."""
import threading
from contextlib import contextmanager

_create_lock = threading.Lock()


@contextmanager
def create_scope():
    """Hold the driver-construction lock around a browser launch."""
    with _create_lock:
        yield
