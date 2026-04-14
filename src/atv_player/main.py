from atv_player.app import AppCoordinator, build_application


def main() -> int:
    app, repo = build_application()
    coordinator = AppCoordinator(repo)
    widget = coordinator.start()
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
