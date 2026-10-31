from fastapi import APIRouter


router = APIRouter()


@router.get("/user/{uid}")
def get_user(uid: int):
    return load_profile(uid)
