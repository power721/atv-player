class DanmakuError(Exception):
    pass


class ProviderNotSupportedError(DanmakuError):
    pass


class DanmakuSearchError(DanmakuError):
    pass


class DanmakuResolveError(DanmakuError):
    pass


class DanmakuEmptyResultError(DanmakuError):
    pass
