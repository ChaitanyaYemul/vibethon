from uuid import uuid4
from types import SimpleNamespace
import pytest
from app.security import token_for, decode_token, hash_password, check_password
from app.errors import ApiError


def test_password_and_signed_token():
    hashed = hash_password("test-password")
    assert check_password("test-password", hashed)
    assert not check_password("wrong", hashed)
    uid = uuid4()
    token = token_for(SimpleNamespace(id=uid, role="rider"))
    assert decode_token(token)["sub"] == str(uid)
    with pytest.raises(ApiError):
        decode_token(token + "tampered")
