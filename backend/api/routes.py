from fastapi import APIRouter

router = APIRouter()


@router.get("/api/scenes")
async def get_scenes():
    return {
        "scenes": [
            {"id": "apartment_v1", "name": "测试公寓"},
        ]
    }


@router.get("/api/health")
async def health_check():
    return {"status": "ok"}
