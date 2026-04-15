import os


# Force a headless Qt backend so pytest-qt does not depend on a live X server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
