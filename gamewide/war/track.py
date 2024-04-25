import aiohttp
import asyncio
import coc
import pendulum as pend
import random

from hashids import Hashids
from datetime import datetime
from msgspec.json import decode
from msgspec import Struct
from pymongo import InsertOne, UpdateOne
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.pool import ProcessPoolExecutor
from typing import List
from utility.classes import MongoDatabase
from .config import GlobalWarTrackingConfig
from utility.keycreation import create_keys
from loguru import logger
from asyncio_throttle import Throttler

config = GlobalWarTrackingConfig()
db_client = MongoDatabase(stats_db_connection=config.stats_mongodb, static_db_connection=config.static_mongodb)
coc_client = coc.Client(key_count=10, throttle_limit=200, cache_max_size=0, raw_attribute=True, timeout=600)


class Members(Struct):
    tag: str

class Clan(Struct):
    tag: str
    members: List[Members]


class War(Struct):
    state: str
    preparationStartTime: str
    endTime: str
    clan: Clan
    opponent: Clan

in_war = set()

store_fails = []

async def broadcast(scheduler: AsyncIOScheduler):
    global in_war
    global store_fails
    x = 1
    keys = await create_keys([config.coc_email.format(x=x) for x in range(config.min_coc_email, config.max_coc_email + 1)], [config.coc_password] * config.max_coc_email)

    throttler = Throttler(rate_limit=1200, period=1)
    print(f"{len(list(keys))} keys")
    await coc_client.login_with_tokens(*list(keys))

    while True:
        api_fails = 0

        async def fetch(url, session: aiohttp.ClientSession, headers, tag, throttler: Throttler):
            async with throttler:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        return ((await response.read()), tag)
                    elif response.status == 403:
                        return (403, 403)
                    return (None, None)

        bot_clan_tags = await db_client.clans_db.distinct("tag")
        size_break = 60_000

        if x % 30 != 0:
            right_now = datetime.now().timestamp()
            one_week_ago = int(right_now) - (604800 * 4)

            pipeline = [
                {"$match":
                    {"$and": [
                        {"endTime": {"$gte": one_week_ago}},
                        {"type": {"$ne": "cwl"}}
                    ]
                    }},
                {"$group": {"_id": "$clans"}}]
            results = await db_client.clan_wars.aggregate(pipeline).to_list(length=None)
            clan_tags = []
            for result in results:
                clan_tags.extend(result.get("_id", []))

            combined_tags = set(clan_tags + bot_clan_tags)
            all_tags = list([tag for tag in combined_tags if tag not in in_war])
        else:
            pipeline = [{"$match": {"openWarLog": True}}, {"$group": {"_id": "$tag"}}]
            all_tags = [x["_id"] for x in (await db_client.global_clans.aggregate(pipeline).to_list(length=None))]
            all_tags = [tag for tag in all_tags if tag not in in_war] + bot_clan_tags
            all_tags = list(set(all_tags))


        logger.info(f"{len(all_tags)} tags")
        all_tags = [all_tags[i:i + size_break] for i in range(0, len(all_tags), size_break)]
        ones_that_tried_again = []

        timers_alr_captured = set()
        if x == 1:
            right_now = datetime.now().timestamp()
            one_week_ago = int(right_now)
            pipeline = [{"$match": {"$and": [
                {"endTime": {"$gte": one_week_ago}},
                {"data": {"$eq": None}}
            ]}}, {"$group": {"_id": "$war_id"}}]
            results = await db_client.clan_wars.aggregate(pipeline).to_list(length=None)
            for result in results:
                timers_alr_captured.add(result.get("_id"))

        x += 1
        for count, tag_group in enumerate(all_tags, 1):
            logger.info(f"Group {count}/{len(all_tags)}")
            tasks = []
            connector = aiohttp.TCPConnector(limit=500, ttl_dns_cache=600)
            async with aiohttp.ClientSession(connector=connector) as session:
                for tag in tag_group:
                    if tag not in in_war:
                        keys.rotate(1)
                        tasks.append(fetch(f"https://api.clashofclans.com/v1/clans/{tag.replace('#', '%23')}/currentwar", session, {"Authorization": f"Bearer {keys[0]}"}, tag, throttler=throttler))
                responses = await asyncio.gather(*tasks, return_exceptions=True)
                await session.close()

            responses = [r for r in responses if type(r) is tuple]
            changes = []
            war_timers = []
            for response, tag in responses:
                # we shouldnt have completely invalid tags, they all existed at some point
                if response is None or response == 403:
                    if response is None:
                        api_fails += 1
                    continue
                try:
                    war = decode(response, type=War)
                except:
                    continue

                if war.state != "notInWar":
                    war_end = coc.Timestamp(data=war.endTime)
                    run_time = war_end.time.replace(tzinfo=pend.UTC)
                    if war_end.seconds_until < 0:
                        continue
                    war_prep = coc.Timestamp(data=war.preparationStartTime)
                    war_prep = war_prep.time.replace(tzinfo=pend.UTC)

                    opponent_tag = war.opponent.tag if war.opponent.tag != tag else war.clan.tag
                    in_war.add(tag)
                    in_war.add(opponent_tag)
                    war_unique_id = "-".join(sorted([war.clan.tag, war.opponent.tag])) + f"-{int(war_prep.timestamp())}"
                    if war_unique_id not in timers_alr_captured:
                        for member in war.clan.members + war.opponent.members:
                            war_timers.append(UpdateOne({"_id" : member.tag}, {"$set" : {"clans" : [war.clan.tag, war.opponent.tag], "time" : war_end.time}}, upsert=True))

                        changes.append(InsertOne({"war_id" : war_unique_id,
                                                  "clans" : [tag, opponent_tag],
                                                  "endTime" : int(war_end.time.replace(tzinfo=pend.UTC).timestamp())
                                                  }))
                    #schedule getting war
                    try:
                        scheduler.add_job(store_war, 'date', run_date=run_time, args=[tag, opponent_tag, int(war_prep.timestamp())],
                                          id=f"war_end_{tag}_{opponent_tag}", name=f"{tag}_war_end_{opponent_tag}", misfire_grace_time=1200, max_instances=1)
                    except Exception:
                        ones_that_tried_again.append(tag)
                        pass
            if changes:
                try:
                    await db_client.clan_wars.bulk_write(changes, ordered=False)
                except Exception:
                    pass

            if war_timers:
                try:
                    await db_client.war_timer.bulk_write(war_timers, ordered=False)
                except Exception:
                    pass

            await asyncio.sleep(5)

        if ones_that_tried_again:
            logger.info(f"{len(ones_that_tried_again)} tried again, examples: {ones_that_tried_again[:5]}")

        if api_fails != 0:
            logger.info(f"{api_fails} API call fails")

        logger.info(f"{len(in_war)} clans in war")
        if store_fails:
            f = '\n- '.join([str(s) for s in store_fails])
            logger.info(f"{len(store_fails)} War Store Fails\n"
                        f"Reasons:\n{f}")
            store_fails = []

async def store_war(clan_tag: str, opponent_tag: str, prep_time: int):
    global in_war
    global store_fails

    hashids = Hashids(min_length=7)

    if clan_tag in in_war:
        in_war.remove(clan_tag)
    if opponent_tag in in_war:
        in_war.remove(opponent_tag)

    async def find_active_war(clan_tag: str, opponent_tag: str, prep_time: int):
        async def get_war(clan_tag: str):
            try:
                war = await coc_client.get_clan_war(clan_tag=clan_tag)
                return war
            except (coc.NotFound, coc.errors.Forbidden, coc.errors.PrivateWarLog):
                return "no access"
            except coc.errors.Maintenance:
                return "maintenance"
            except Exception as e:
                logger.error(str(e))
                return "error"

        switched = False
        tries = 0
        while True:
            war = await get_war(clan_tag=clan_tag)

            if isinstance(war, coc.ClanWar):
                if war.state == "warEnded":
                    return war  # Found the completed war
                # Check prep time and retry if needed
                prep_start_timestamp = int(war.preparation_start_time.time.replace(tzinfo=pend.UTC).timestamp())
                if war.preparation_start_time is None or prep_start_timestamp != prep_time:
                    if not switched:
                        clan_tag = opponent_tag
                        switched = True
                        continue  # Try with the opponent's tag
                    else:
                        return None  # Both tags checked, no valid war found
            elif war == "maintenance":
                await asyncio.sleep(15 * 60)  # Wait 15 minutes for maintenance, then continue loop
                continue
            elif war == "error":
                break  # Stop on error
            elif war == "no access":
                if not switched:
                    clan_tag = opponent_tag
                    switched = True
                    continue  # Access issue, switch clan tag
                else:
                    return None  # Both tags checked, no access to either

            await asyncio.sleep(war._response_retry)  # Wait before retry based on response retry attribute
            tries += 1
            if tries == 10:
                break

        return None

    war = await find_active_war(clan_tag=clan_tag, opponent_tag=opponent_tag, prep_time=prep_time)

    if war is None:
        store_fails.append(war)
        return

    war_unique_id = "-".join(sorted([war.clan.tag, war.opponent.tag])) + f"-{int(war.preparation_start_time.time.replace(tzinfo=pend.UTC).timestamp())}"
    
    custom_id = hashids.encode(int(war.preparation_start_time.time.replace(tzinfo=pend.UTC).timestamp()) + int(pend.now(tz=pend.UTC).timestamp()) + random.randint(1000000000, 9999999999))
    await db_client.clan_wars.update_one({"war_id": war_unique_id},
        {"$set" : {
        "custom_id": custom_id,
        "data": war._raw_data,
        "type" : war.type}}, upsert=True)


async def main():
    scheduler = AsyncIOScheduler(timezone=pend.UTC)
    scheduler.start()
    await broadcast(scheduler=scheduler)



