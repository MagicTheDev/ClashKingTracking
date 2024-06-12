import coc
import pendulum as pend
import ujson
import aiohttp
import asyncio

from typing import Optional, List
from collections import deque
from msgspec.json import decode
from msgspec import Struct
from pymongo import UpdateOne, DeleteOne, InsertOne
from aiohttp import TCPConnector, ClientTimeout, ClientSession
from utility.utils import gen_season_date, gen_raid_date
from utility.keycreation import create_keys
from .config import ClanVerifyTrackingConfig
from utility.classes import MongoDatabase
from loguru import logger



async def fetch(url, session: aiohttp.ClientSession, headers):
    async with session.get(url, headers=headers) as response:
        if response.status == 200:
            return (await response.read())
        return None


async def main():
    config = ClanVerifyTrackingConfig()
    db_client = MongoDatabase(stats_db_connection=config.stats_mongodb, static_db_connection=config.static_mongodb)

    coc_client = coc.Client(raw_attribute=True)
    keys: deque = await create_keys([config.coc_email.format(x=x) for x in range(config.min_coc_email, config.max_coc_email + 1)], [config.coc_password] * config.max_coc_email)
    logger.info(f"{len(keys)} keys")
    x = 1

    changes = []
    async for clan in db_client.basic_clan.find({}, {"tag" : 1, "changes" : 1}):
        changes.append(UpdateOne({"_id" : clan.get("tag")}, {"$set" : {"changes" : clan.get("changes", {})}}, upsert=True))

    await db_client.global_clans.bulk_write(changes, ordered=False)
    print("UPDATED CHANGES")
    return

    while True:
        #try:
            ranking_pipeline = [{"$unwind": "$memberList"},
                                {"$match": {"memberList.league": "Legend League"}},
                                {"$project": {"name": "$memberList.name", "tag": "$memberList.tag",
                                              "trophies": "$memberList.trophies", "townhall": "$memberList.townhall", "sort_field" : {"trophies" : "$memberList.trophies", "tag" : "$memberList.tag"}}},
                                {"$unset": ["_id"]},
                                {"$setWindowFields": {
                                    "sortBy": {"sort_field": -1},
                                    "output": {
                                        "rank": {"$rank": {}}
                                    }
                                }},
                                {"$unset" : ["sort_field"]},
                                {"$out": {"db": "new_looper", "coll": "legend_rankings"}}
                                ]
            #await db_client.global_clans.aggregate(ranking_pipeline).to_list(length=None)
            logger.info("UPDATED RANKING")

            keys = deque(keys)
            if x % 20 == 0:
                pipeline = [{"$match" : {"$or" : [{"members" : {"$lt" : 10}}, {"level" : {"$lt" : 3}}, {"capitalLeague" : "Unranked"}]}}, { "$group" : { "_id" : "$tag" } } ]
            else:
                pipeline = [{"$match": {"$nor" : [{"members" : {"$lt" : 10}}, {"level" : {"$lt" : 3}}, {"capitalLeague" : "Unranked"}]}}, {"$group": {"_id": "$tag"}}]

            pipeline = [{"$match": {}}, {"$group": {"_id": "$tag"}}]
            x += 1
            all_tags = [x["_id"] for x in (await db_client.basic_clan.aggregate(pipeline).to_list(length=None))]
            bot_clan_tags = await db_client.clans_db.distinct("tag")
            all_tags = list(set(all_tags + bot_clan_tags))

            logger.info(f"{len(all_tags)} tags")
            size_break = 25000
            all_tags = [all_tags[i:i + size_break] for i in range(0, len(all_tags), size_break)]

            for tag_group in all_tags:
                #try:
                    tasks = []
                    connector = TCPConnector(limit=500, enable_cleanup_closed=True)
                    timeout = ClientTimeout(total=1800)
                    async with ClientSession(connector=connector, timeout=timeout) as session:
                        for tag in tag_group:
                            keys.rotate(1)
                            tasks.append(fetch(f"https://api.clashofclans.com/v1/clans/{tag.replace('#', '%23')}", session, {"Authorization": f"Bearer {keys[0]}"}))
                        responses = await asyncio.gather(*tasks)
                        await session.close()
                    logger.info(f"fetched {len(responses)} responses")
                    changes = []
                    join_leave_changes = []

                    raid_week = gen_raid_date()
                    season = gen_season_date()
                    '''clan_group_members = await db_client.global_clans.find({"tag" : {"$in" : tag_group}}, {"tag" : 1, "_id" : 0, "data" : 1}).to_list(length=None)
                    clan_group_members = {x.get("data").get("tag") : x.get("memberList", []) for x in clan_group_members}'''
                    for response in responses: #type: bytes
                        # we shouldnt have completely invalid tags, they all existed at some point
                        if response is None:
                            continue

                        clan = ujson.loads(response)
                        if clan.get("members") == 0:
                            await db_client.deleted_clans.insert_one(clan)
                            #changes.append(DeleteOne({"tag": clan.tag}))
                        else:
                            clan = coc.Clan(data=clan, client=coc_client)
                            '''members = []
                            if clan.tag in clan_group_members:
                                clan_member_list = [m for m in clan_group_members.get(clan.tag)]
                                new_joins = [player for player in clan.memberList if player.tag not in set(p.tag for p in clan_member_list)]
                                new_leaves = [player for player in clan_member_list if player.tag not in set(p.tag for p in clan.memberList)]
                                for join in new_joins:
                                    join_leave_changes.append(InsertOne({
                                        "type" : "join",
                                        "clan" : clan.tag,
                                        "time" : pend.now(tz=pend.UTC),
                                        "tag" : join.tag,
                                        "name" : join.name,
                                        "th" : join.townHallLevel
                                    }))

                                for leave in new_leaves:
                                    join_leave_changes.append(InsertOne({
                                        "type" : "leave",
                                        "clan" : clan.tag,
                                        "time" : pend.now(tz=pend.UTC),
                                        "tag" : leave.tag,
                                        "name" : leave.name,
                                        "th" : leave.townHallLevel
                                    }))

                            for member in clan.memberList:
                                members.append({"name": member.name, "tag" : member.tag, "role" : member.role, "expLevel" : member.expLevel, "trophies" : member.trophies,
                                                "townhall" : member.townHallLevel, "league" : member.league.name,
                                        "builderTrophies" : member.builderBaseTrophies, "donations" : member.donations, "donationsReceived" : member.donationsReceived})'''
                            changes.append(UpdateOne({"_id": clan.tag},
                                                          {"$set":
                                                               {
                                                                "isValid" : clan.member_count >= 5,
                                                                f"changes.clanCapital.{raid_week}": {"trophies" : clan.capital_points, "league" : clan.capital_league.name},
                                                                f"changes.clanWarLeague.{season}": {"league": clan.war_league.name},
                                                                "data" : clan._raw_data
                                                                },
                                                           },
                                                          upsert=True))

                    if changes:
                        await db_client.global_clans.bulk_write(changes, ordered=False)
                        logger.info(f"Made {len(changes)} clan changes")

                    if join_leave_changes:
                        await db_client.join_leave_history.bulk_write(join_leave_changes, ordered=False)
                        logger.info(f"Made {len(join_leave_changes)} join/leave changes")
                #except Exception:
                    #continue

        #except Exception:
            #continue


