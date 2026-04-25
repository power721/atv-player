class SecSpiderError(Exception):
    pass


class SecSpiderFormatError(SecSpiderError):
    pass


class SecSpiderSignatureError(SecSpiderError):
    pass


class SecSpiderKeyError(SecSpiderError):
    pass


class SecSpiderDecryptError(SecSpiderError):
    pass


class SecSpiderHashError(SecSpiderError):
    pass


class SecSpiderRuntimeError(SecSpiderError):
    pass
