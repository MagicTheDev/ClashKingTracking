import asyncio
from kafka import KafkaProducer
from config import BotClanTrackingConfig
from utils import clan_track
from utility.utils import initialize_coc_client
from utility.classes import MongoDatabase

async def main():
    """Main function for clan tracking."""
    config = BotClanTrackingConfig()
    producer = KafkaProducer(bootstrap_servers=["85.10.200.219:9092"])
    db_client = MongoDatabase(
        stats_db_connection=config.stats_mongodb,
        static_db_connection=config.static_mongodb,
    )
    coc_client = await initialize_coc_client(config)

    while True:
        try:
            # Fetch all clan tags from the database
            clan_tags = await db_client.clans_db.distinct("tag")
            for clan_tag in clan_tags:
                await clan_track(clan_tag, coc_client, producer)
            print("Finished clan tracking for all clans.")
        except Exception as e:
            print(f"Error in clan tracking: {e}")
        await asyncio.sleep(180)  # Adjust interval as needed

asyncio.run(main())
