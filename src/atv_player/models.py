from dataclasses import dataclass


@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    last_path: str = "/"
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
