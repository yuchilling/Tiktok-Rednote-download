# -*- coding: utf-8 -*-
"""抖音 web 端 `a_bogus` 签名（纯标准库移植版）。

原算法为抖音前端混淆 JS，这里采用社区逆向结果
(https://github.com/amxsa/douyin_abogus_python ，MIT 风格的无限制公开代码)
的纯 Python 版本，并把原本依赖 gmssl 的 SM3 换成 ``hashlib.new("sm3")``，
因此**不需要任何第三方库**。

用法::

    from app.sign.abogus import ABogus

    signer = ABogus(user_agent)
    params = {"device_platform": "webapp", "aweme_id": "7123..."}
    params["a_bogus"] = signer.sign(params)

注意：`a_bogus` 只解决“签名”这一层；接口还会校验 Cookie 里的
`ttwid` / `msToken` / `UIFID` 等风控指纹，缺了照样 403。
"""
from __future__ import annotations

import hashlib
from random import choice, randint, random
from time import time
from urllib.parse import urlencode

__all__ = ["ABogus", "sm3_hex"]


def sm3_hex(data: bytes) -> str:
    try:
        h = hashlib.new("sm3")
    except ValueError as exc:  # pragma: no cover - 取决于 OpenSSL 能力
        raise RuntimeError("当前 Python 的 OpenSSL 不支持 SM3，无法生成 a_bogus 签名") from exc
    h.update(data)
    return h.hexdigest()


class ABogus:
    __end_string = "cus"
    __str = {
        "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
        "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
        "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
    }

    def __init__(self, user_agent: str = "", platform: str = "Win32"):
        self.user_agent = user_agent
        self.ua_code = self.generate_ua_code(user_agent)
        self.browser = self.generate_browser_info(platform)
        self.browser_len = len(self.browser)
        self.browser_code = self.char_code_at(self.browser)

    # ---------- 基础工具 ----------
    def generate_ua_code(self, user_agent: str) -> list[int]:
        numbers = [0.00390625, 1, 14]
        key_string = "".join(chr(int(num)) for num in numbers)
        return self.sm3_to_array(self.generate_result(self.rc4_encrypt(user_agent, key_string), "s3"))

    def list_1(self, a=170, b=85, c=45) -> list[int]:
        return self.random_list(a, b, 1, 2, 5, c & a)

    def list_2(self, a=170, b=85) -> list[int]:
        return self.random_list(a, b, 1, 0, 0, 0)

    def list_3(self, a=170, b=85) -> list[int]:
        return self.random_list(a, b, 1, 0, 5, 0)

    def random_list(self, b=170, c=85, d=0, e=0, f=0, g=0) -> list[int]:
        r = random() * 10000
        v = [r, int(r) & 255, int(r) >> 8]
        s = v[1] & b | d
        v.append(s)
        s = v[1] & c | e
        v.append(s)
        s = v[2] & b | f
        v.append(s)
        s = v[2] & c | g
        v.append(s)
        return v[-4:]

    def from_char_code(self, *args) -> str:
        return "".join(chr(code) for code in args)

    def generate_string_1(self) -> str:
        return (self.from_char_code(*self.list_1())
                + self.from_char_code(*self.list_2())
                + self.from_char_code(*self.list_3()))

    def generate_string_2(self, url_params: str, method: str = "GET") -> str:
        a = self.generate_string_2_list(url_params, method)
        e = self.end_check_num(a)
        a.extend(self.browser_code)
        a.append(e)
        return self.rc4_encrypt(self.from_char_code(*a), "y")

    def generate_string_2_list(self, url_params: str, method: str = "GET") -> list[int]:
        start_time = int(time() * 1000)
        end_time = start_time + randint(4, 8)
        params_array = self.generate_params_code(url_params)
        method_array = self.generate_method_code(method)
        return self.list_4(
            (end_time >> 24) & 255, params_array[21], self.ua_code[23],
            (end_time >> 16) & 255, params_array[22], self.ua_code[24],
            (end_time >> 8) & 255, (end_time >> 0) & 255,
            (start_time >> 24) & 255, (start_time >> 16) & 255,
            (start_time >> 8) & 255, (start_time >> 0) & 255,
            method_array[21], method_array[22],
            int(end_time / 256 / 256 / 256 / 256) >> 0,
            int(start_time / 256 / 256 / 256 / 256) >> 0,
            self.browser_len,
        )

    @staticmethod
    def list_4(a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, r) -> list[int]:  # noqa: PLR0913
        return [
            44, a, 0, 0, 0, 0, 24, b,
            n, 0, c, d, 0, 0, 0, 1, 0, 239,
            e, o, f, g, 0, 0, 0, 0, h, 0, 0, 14,
            i, j, 0, k, m, 3, p, 1, q, 1, r,
            0, 0, 0,
        ]

    @staticmethod
    def end_check_num(a: list[int]) -> int:
        r = 0
        for i in a:
            r ^= i
        return r

    @staticmethod
    def char_code_at(s: str) -> list[int]:
        return [ord(ch) for ch in s]

    def generate_result(self, s: str, e: str = "s4") -> str:
        r: list[str] = []
        for i in range(0, len(s), 3):
            if i + 2 < len(s):
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8) | ord(s[i + 2])
            elif i + 1 < len(s):
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8)
            else:
                n = ord(s[i]) << 16
            for j, k in zip(range(18, -1, -6), (0xFC0000, 0x03F000, 0x0FC0, 0x3F)):
                if j == 6 and i + 1 >= len(s):
                    break
                if j == 0 and i + 2 >= len(s):
                    break
                r.append(self.__str[e][(n & k) >> j])
        r.append("=" * ((4 - len(r) % 4) % 4))
        return "".join(r)

    def generate_method_code(self, method: str = "GET") -> list[int]:
        return self.sm3_to_array(bytes(self.sm3_to_array(method + self.__end_string)))

    def generate_params_code(self, params: str) -> list[int]:
        return self.sm3_to_array(bytes(self.sm3_to_array(params + self.__end_string)))

    @staticmethod
    def sm3_to_array(data) -> list[int]:
        if isinstance(data, str):
            b = data.encode("utf-8")
        else:
            b = bytes(data)
        h = sm3_hex(b)
        return [int(h[i: i + 2], 16) for i in range(0, len(h), 2)]

    @staticmethod
    def generate_browser_info(platform: str = "Win32") -> str:
        inner_width = randint(1280, 1920)
        inner_height = randint(720, 1080)
        outer_width = randint(inner_width, 1920)
        outer_height = randint(inner_height, 1080)
        screen_x = 0
        screen_y = choice((0, 30))
        value_list = [
            inner_width, inner_height, outer_width, outer_height,
            screen_x, screen_y, 0, 0,
            outer_width, outer_height, outer_width, outer_height,
            inner_width, inner_height, 24, 24, platform,
        ]
        return "|".join(str(i) for i in value_list)

    @staticmethod
    def rc4_encrypt(plaintext: str, key: str) -> str:
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + ord(key[i % len(key)])) % 256
            s[i], s[j] = s[j], s[i]
        i = 0
        j = 0
        cipher = []
        for k in range(len(plaintext)):
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            t = (s[i] + s[j]) % 256
            cipher.append(chr(s[t] ^ ord(plaintext[k])))
        return "".join(cipher)

    def sign(self, url_params: dict | str) -> str:
        """返回 `a_bogus` 字符串（参数需与请求里最终发送的顺序/内容一致）。"""
        string_1 = self.generate_string_1()
        string_2 = self.generate_string_2(urlencode(url_params))
        return self.generate_result(string_1 + string_2, "s4")

    # 兼容原项目的命名
    generate_a_bogus = sign
