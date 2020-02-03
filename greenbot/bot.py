import logging
import asyncio
import sys

from greenbot.managers.schedule import ScheduleManager
from greenbot.managers.db import DBManager
from greenbot.managers.redis import RedisManager
from greenbot.managers.handler import HandlerManager
from greenbot.managers.discord_bot import DiscordBotManager
from greenbot.migration.db import DatabaseMigratable
from greenbot.migration.migrate import Migration
from greenbot.utils import wait_for_redis_data_loaded

import greenbot.migration_revisions.db

log = logging.getLogger(__name__)


class Bot:
    """
    Main class for the discord bot
    """

    def __init__(self, config, args):
        self.config = config
        self.args = args
        self.private_loop = asyncio.get_event_loop()

        self.discord_token = self.config["main"]["discord_token"]

        ScheduleManager.init()
        DBManager.init(self.config["main"]["db"])

        # redis
        redis_options = {}
        if "redis" in config:
            redis_options = dict(config.items("redis"))
        RedisManager.init(**redis_options)
        wait_for_redis_data_loaded(RedisManager.get())

        # SQL migrations
        with DBManager.create_dbapi_connection_scope() as sql_conn:
            sql_migratable = DatabaseMigratable(sql_conn)
            sql_migration = Migration(sql_migratable, greenbot.migration_revisions.db, self)
            sql_migration.run()

        HandlerManager.init_handlers()

        settings = {
            "discord_token": self.discord_token,
            "channels": self.config["discord"]["channels_to_listen_in"].split(" "),
            "command_prefix": self.config["discord"]["command_prefix"],
            "admin_roles": [{"role_id": self.config[role]["role_id"], "level": self.config[role]["level"]} for role in self.config["discord"]["admin_roles"]]
        }

        self.discord_bot = DiscordBotManager(bot=self, settings=settings, redis=RedisManager.get(), private_loop=self.private_loop)
        
        HandlerManager.trigger("manager_loaded")

    def quit_bot(self):
        HandlerManager.trigger("on_quit")
        try:
            ScheduleManager.base_scheduler.print_jobs()
            ScheduleManager.base_scheduler.shutdown(wait=False)
        except:
            log.exception("Error while shutting down the apscheduler")
        sys.exit(0)

    def connect(self):
        self.discord_bot.connect()

    def start(self):
        self.private_loop.run_forever()

    def ban(self, user_id, timeout_in_seconds=0, reason=None, delete_message_days=0):
        self.discord_bot.ban(user_id=user_id, timeout_in_seconds=timeout_in_seconds, reason=reason, delete_message_days=delete_message_days)

    def unban(self, user_id, reason=None):
        self.discord_bot.unban(user_id=user_id, reason=reason)
    