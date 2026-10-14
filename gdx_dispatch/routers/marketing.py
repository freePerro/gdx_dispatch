from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api", tags=["marketing"])


class ReferralCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    referrer_customer_id: str = Field(..., min_length=1, max_length=64)
    referee_name: str = Field(..., min_length=1, max_length=120)
    referee_phone: str = Field(..., min_length=1, max_length=30)


# The review and referral endpoints live in routers/reviews.py and routers/referrals.py.
