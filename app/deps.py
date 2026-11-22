from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account no longer exists.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
           status_code=status.HTTP_403_FORBIDDEN,
           detail="This account has been deactivated by the Head Librarian.",
        ) 
    return user


def require_librarian(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "librarian":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Librarian privileges are required for this action.",
        )
    return current_user

def require_head_librarian(
    current_user: User = Depends(require_librarian),
) -> User:
    if not current_user.is_head_librarian:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Head Librarian privileges required for staff management.",
        )
    return current_user