"""a_bogus 签名（抖音 web 接口必需），纯标准库实现。"""

from .abogus import ABogus


def sign(url_params: dict | str, user_agent: str = "") -> str:
    """便捷函数：用默认 UA 现签一个 a_bogus。"""
    return ABogus(user_agent).sign(url_params)


__all__ = ["ABogus", "sign"]
