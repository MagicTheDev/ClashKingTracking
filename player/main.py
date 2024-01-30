import motor.motor_asyncio
import asyncio

from loguru import logger
from collections import deque
from redis import asyncio as redis
from .config import Config
from .track import main
from ..utils import create_keys


if __name__ == "__main__":
    config = Config()
    loop = asyncio.get_event_loop()

    stats_mongo_client = motor.motor_asyncio.AsyncIOMotorClient(config.stats_mongodb)
    static_mongo_client = motor.motor_asyncio.AsyncIOMotorClient(config.static_mongodb)

    redis_host = redis.Redis(host=config.redis_ip, port=6379, db=0, password=config.redis_pw, decode_responses=False, max_connections=5000)
    keys = create_keys([f"apiclashofclans+test{x}@gmail.com" for x in range(config.min_coc_email, config.max_coc_email + 1)], [config.coc_password] * config.max_coc_email)
    logger.info(f"{len(keys)} keys created")
    keys = deque(keys)
    loop.create_task(main(keys=keys, cache=redis_host, stats_mongo_client=stats_mongo_client, static_mongo_client=static_mongo_client, config=config))
    loop.run_forever()
