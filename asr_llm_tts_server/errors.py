"""服务器内部统一异常类型。"""


class VoiceServerError(Exception):
    """语音服务器基础异常。"""


class ConfigError(VoiceServerError):
    """配置缺失或配置值无法使用。"""


class ProviderError(VoiceServerError):
    """云端供应商接口调用失败。"""


class CancelledError(VoiceServerError):
    """本轮对话被客户端打断取消。"""
