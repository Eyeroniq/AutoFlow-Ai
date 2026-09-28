from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory storage for Phase 1. Point `storage_uri` at settings.REDIS_URL once
# the API runs with more than one process.
limiter = Limiter(key_func=get_remote_address)
