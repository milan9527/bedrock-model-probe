import os
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException


@lru_cache
def jwks():
    return jwt.PyJWKClient(f"{os.environ['JWT_ISSUER']}/.well-known/jwks.json")


def authorize(authorization: str = Header(default="")):
    if os.getenv("LOCAL_DEV") == "1":
        return {"sub": "local", "cognito:groups": ["probe-admins"]}
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to view probe data")
    token = authorization[7:]
    try:
        key = jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=os.environ["JWT_ISSUER"],
                            options={"verify_aud": False, "require": ["exp", "iat", "sub"]})
        if claims.get("token_use") != "access":
            raise ValueError("Expected a Cognito access token")
        if os.getenv("JWT_CLIENT_ID") and claims.get("client_id") != os.environ["JWT_CLIENT_ID"]:
            raise ValueError("Unexpected app client")
        return claims
    except Exception:
        raise HTTPException(401, "Invalid or expired sign-in") from None


def can_manage(claims):
    return "probe-admins" in claims.get("cognito:groups", [])


def administrator(claims=Depends(authorize)):
    if not can_manage(claims):
        raise HTTPException(403, "Probe administrator access is required")
    return claims
