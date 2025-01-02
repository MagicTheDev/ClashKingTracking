import asyncio

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration

from utility.config import Config
from utility.config import Config, TrackingType
from asyncio_throttle import Throttler
import coc
import pendulum as pend
from loguru import logger
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import ujson

from utility.utils import sentry_filter

import ujson
from collections import defaultdict, deque

class Tracking():
    def __init__(self, max_concurrent_requests=1000, batch_size=500, throttle_speed=1000, tracker_type=TrackingType):
        self.config = Config(config_type=tracker_type)
        self.db_client = None
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self.message_count = 0
        self.iterations = 0
        self.batch_size = batch_size
        self.throttler = Throttler(throttle_speed)
        self.coc_client = None
        self.redis = None
        self.logger = logger
        self.http_session = None
        self.scheduler = None
        self.kafka = None
        self.type = tracker_type
        self.max_stats_size = 10_000
        self.request_stats = defaultdict(lambda: deque(maxlen=self.max_stats_size))

    async def initialize(self):
        """Initialise the tracker with dependencies."""
        await self.config.initialize()
        self.db_client = self.config.get_mongo_database()
        self.redis = self.config.get_redis_client()
        self.coc_client = self.config.coc_client

        self.kafka = self.config.get_kafka_producer()

        connector = aiohttp.TCPConnector(limit=1200, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=1800)
        self.http_session = aiohttp.ClientSession(connector=connector, timeout=timeout, json_serialize=ujson.dumps)

        self.scheduler = AsyncIOScheduler(timezone=pend.UTC)

    async def track(self, items):
        """Track items in batches."""
        self.message_count = 0  # Reset message count
        for i in range(0, len(items), self.batch_size):
            batch = items[i:i + self.batch_size]
            print(f"Processing batch {i // self.batch_size + 1} of {len(items) // self.batch_size + 1}.")
            await self._track_batch(batch)

        sentry_sdk.add_breadcrumb(message="Finished tracking all clans.", level="info")
        print("Finished tracking all clans.")


    async def fetch(self, url: str, tag: str, json=False):
        async with self.throttler:
            self.keys.rotate(1)
            self.request_stats[url].append({"time" : pend.now(tz=pend.UTC).timestamp()})
            async with self.http_session.get(url, headers={'Authorization': f'Bearer {self.keys[0]}'}) as response:
                if response.status == 200:
                    if not json:
                        return (await response.read(), tag)
                    return (await response.json(), tag)
                return (None, None)

    async def _track_batch(self, batch):
        """Track a batch of items."""
        async with self.semaphore:
            tasks = [self._track_item(item) for item in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    self._handle_exception("Error in tracking task", result)
        print(f"Finished tracking batch of {len(batch)} clans.")  # Added print

    async def _track_item(self, item):
        """Override this method in child classes."""
        raise NotImplementedError("This method should be overridden in child classes.")

    def _send_to_kafka(self, topic, key, data):
        """Helper to send data to Kafka."""
        sentry_sdk.add_breadcrumb(
            message=f"Sending data to Kafka: topic={topic}, key={key}",
            data={"data_preview": data if self.config.is_beta else "Data suppressed in production"},
            level="info",
        )
        self.kafka.send(
            topic=topic,
            value=ujson.dumps(data).encode('utf-8'),
            key=key.encode('utf-8'),
            timestamp_ms=int(pend.now(tz=pend.UTC).timestamp() * 1000),
        )
        self.message_count += 1

    @staticmethod
    def _handle_exception(message, exception):
        """Handle exceptions by logging to Sentry and console."""
        sentry_sdk.capture_exception(exception)
        print(f"{message}: {exception}")

    @staticmethod
    def gen_raid_date():
        now = pend.now(tz=pend.UTC)
        current_dayofweek = now.day_of_week  # Monday = 0, Sunday = 6
        if (
                (current_dayofweek == 4 and now.hour >= 7)  # Friday after 7 AM UTC
                or (current_dayofweek == 5)  # Saturday
                or (current_dayofweek == 6)  # Sunday
                or (current_dayofweek == 0 and now.hour < 7)  # Monday before 7 AM UTC
        ):
            raid_date = now.subtract(days=(current_dayofweek - 4 if current_dayofweek >= 4 else 0)).date()
        else:
            forward = 4 - current_dayofweek  # Days until next Friday
            raid_date = now.add(days=forward).date()
        return str(raid_date)

    @staticmethod
    def gen_season_date():
        end = coc.utils.get_season_end().astimezone(pend.UTC)
        month = f"{end.month:02}"
        return f"{end.year}-{month}"

    @staticmethod
    def gen_legend_date():
        now = pend.now(tz=pend.UTC)
        date = now.subtract(days=1).date() if now.hour < 5 else now.date()
        return str(date)

    @staticmethod
    def gen_games_season():
        now = pend.now(tz=pend.UTC)
        month = f"{now.month:02}"  # Ensure two-digit month
        return f"{now.year}-{month}"

    @staticmethod
    def is_raids():
        now = pend.now(tz=pend.UTC)
        current_dayofweek = now.day_of_week  # Monday = 0, Sunday = 6
        return (
                (current_dayofweek == 4 and now.hour >= 7)  # Friday after 7 AM UTC
                or (current_dayofweek == 5)  # Saturday
                or (current_dayofweek == 6)  # Sunday
                or (current_dayofweek == 0 and now.hour < 9)  # Monday before 9 AM UTC
        )

    async def run(self, tracker_class, config_type="bot_clan", loop_interval=60, is_tracking_allowed=None):
        """Main function for generic tracking."""
        tracker = tracker_class(self.type)
        await tracker.initialize()

        sentry_sdk.init(
            dsn=self.config.sentry_dsn,
            traces_sample_rate=1.0,
            integrations=[AsyncioIntegration()],
            profiles_sample_rate=1.0,
            environment="production" if self.config.is_main else "beta",
            before_send=sentry_filter,
        )

        try:
            async with tracker.http_session:
                while True:
                    if is_tracking_allowed is None or is_tracking_allowed():
                        clan_tags = await tracker.db_client.clans_db.distinct("tag")
                        start_time = pend.now(tz=pend.UTC)
                        await tracker.track(clan_tags)
                        elapsed_time = pend.now(tz=pend.UTC) - start_time
                        tracker.logger.info(
                            f"Tracked {len(clan_tags)} clans in {elapsed_time.in_seconds()} seconds. "
                            f"Messages sent: {tracker.message_count} "
                            f"({tracker.message_count / elapsed_time.in_seconds()} msg/s)."
                        )
                    else:
                        tracker.logger.info("Tracking not allowed. Sleeping until the next interval.")
                    await asyncio.sleep(loop_interval)
        except KeyboardInterrupt:
            tracker.logger.info("Tracking interrupted by user.")
        finally:
            await tracker.coc_client.close()
            tracker.logger.info("Tracking completed.")

