from datetime import timedelta
import bcrypt
import jwt
from slowapi import Limiter
from app import clock
from app.config import settings
from app.errors import ApiError


def ip_key(request):
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")


limiter = Limiter(key_func=ip_key, default_limits=["120/minute"])


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def token_for(user):
    return jwt.encode({"sub": str(user.id), "role": user.role,
                       "exp": clock.real_now() + timedelta(minutes=settings.jwt_exp_minutes)},
                      settings.jwt_secret, algorithm="HS256")


def decode_token(token):
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["sub", "role", "exp"]})
    except jwt.PyJWTError:
        raise ApiError(401, "UNAUTHORIZED", "Invalid or expired token") from None
