"""
Шифрование ключей Манго в таблице clients. Симметричный Fernet, ключ — из
переменной окружения ENCRYPTION_KEY (сгенерировать: Fernet.generate_key()).

Ключи Манго — это доступ к чужой АТС, хранить их открытым текстом нельзя.
"""

import os
from functools import lru_cache

from cryptography.fernet import Fernet


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.environ["ENCRYPTION_KEY"]
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
