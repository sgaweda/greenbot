import collections
import json
import logging
import sys

import regex as re
import requests

from greenbot.managers.schedule import ScheduleManager

log = logging.getLogger(__name__)


class ActionParser:
    bot = None

    @staticmethod
    def parse(raw_data=None, data=None, command=""):
        from greenbot.dispatch import Dispatch

        if not data:
            data = json.loads(raw_data)

        if data["type"] == "channelmessage":
            action = SayAction(data["message"], ActionParser.bot)
        elif data["type"] == "privatemessage":
            action = MeAction(data["message"], ActionParser.bot)
        elif data["type"] == "func":
            try:
                action = FuncAction(getattr(Dispatch, data["cb"]))
            except AttributeError as e:
                log.error(f'AttributeError caught when parsing action for action "{command}": {e}')
                return None
        elif data["type"] == "multi":
            action = MultiAction(data["args"], data["default"])
        else:
            raise Exception(f"Unknown action type: {data['type']}")

        return action


def apply_substitutions(text, substitutions, bot, extra):
    for needle, sub in substitutions.items():
        if sub.key and sub.argument:
            param = sub.key
            extra["argument"] = MessageAction.get_argument_value(extra["message"], sub.argument - 1)
        elif sub.key:
            param = sub.key
        elif sub.argument:
            param = MessageAction.get_argument_value(extra["message"], sub.argument - 1)
        else:
            log.error("Unknown param for response.")
            continue
        value = sub.cb(param, extra)
        if value is None:
            return None
        try:
            for f in sub.filters:
                value = bot.apply_filter(value, f)
        except:
            log.exception("Exception caught in filter application")
        if value is None:
            return None
        text = text.replace(needle, str(value))

    return text


class IfSubstitution:
    def __call__(self, key, extra={}):
        if self.sub.key is None:
            msg = MessageAction.get_argument_value(extra.get("message", ""), self.sub.argument - 1)
            if msg:
                return self.get_true_response(extra)

            return self.get_false_response(extra)

        res = self.sub.cb(self.sub.key, extra)
        if res:
            return self.get_true_response(extra)

        return self.get_false_response(extra)

    def get_true_response(self, extra):
        return apply_substitutions(self.true_response, self.true_subs, self.bot, extra)

    def get_false_response(self, extra):
        return apply_substitutions(self.false_response, self.false_subs, self.bot, extra)

    def __init__(self, key, arguments, bot):
        self.bot = bot
        subs = get_substitutions(key, bot)
        if len(subs) == 1:
            self.sub = list(subs.values())[0]
        else:
            subs = get_argument_substitutions(key)
            if len(subs) == 1:
                self.sub = subs[0]
            else:
                self.sub = None
        self.true_response = arguments[0][2:-1] if arguments else "Yes"
        self.false_response = arguments[1][2:-1] if len(arguments) > 1 else "No"

        self.true_subs = get_substitutions(self.true_response, bot)
        self.false_subs = get_substitutions(self.false_response, bot)


class Substitution:
    argument_substitution_regex = re.compile(r"\$\((\d+)\)")
    substitution_regex = re.compile(
        r'\$\(([a-z_]+)(\;[0-9]+)?(\:[\w\.\/ -]+|\:\$\([\w_:;\._\/ -]+\))?(\|[\w]+(\([\w%:/ +-]+\))?)*(\,[\'"]{1}[\w \|$;_\-:()\.]+[\'"]{1}){0,2}\)'
    )
    # https://stackoverflow.com/a/7109208
    urlfetch_substitution_regex = re.compile(r"\$\(urlfetch ([A-Za-z0-9\-._~:/?#\[\]@!$%&\'()*+,;=]+)\)")
    urlfetch_substitution_regex_all = re.compile(r"\$\(urlfetch (.+?)\)")

    def __init__(self, cb, needle, key=None, argument=None, filters=[]):
        self.cb = cb
        self.key = key
        self.argument = argument
        self.filters = filters
        self.needle = needle


class SubstitutionFilter:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class BaseAction:
    type = None
    subtype = None

    def reset(self):
        pass


class MultiAction(BaseAction):
    type = "multi"

    def __init__(self, args, default=None, fallback=None):
        from greenbot.models.command import Command

        self.commands = {}
        self.default = default
        self.fallback = fallback

        for command in args:
            cmd = Command.from_json(command)
            for alias in command["command"].split("|"):
                if alias not in self.commands:
                    self.commands[alias] = cmd
                else:
                    log.error(f"Alias {alias} for this multiaction is already in use.")

        import copy

        self.original_commands = copy.copy(self.commands)

    def reset(self):
        import copy

        self.commands = copy.copy(self.original_commands)

    def __iadd__(self, other):
        if other is not None and other.type == "multi":
            self.commands.update(other.commands)
        return self

    @classmethod
    def ready_built(cls, commands, default=None, fallback=None):
        """ Useful if you already have a dictionary
        with commands pre-built.
        """

        multiaction = cls(args=[], default=default, fallback=fallback)
        multiaction.commands = commands
        import copy

        multiaction.original_commands = copy.copy(commands)
        return multiaction

    def run(self, bot, user_id, channel_id, message, whisper, args):
        """ If there is more text sent to the multicommand after the
        initial alias, we _ALWAYS_ assume it's trying the subaction command.
        If the extra text was not a valid command, we try to run the fallback command.
        In case there's no extra text sent, we will try to run the default command.
        """

        cmd = None
        if message:
            msg_lower_parts = message.lower().split(" ")
            command = msg_lower_parts[0]
            cmd = self.commands.get(command, None)
            extra_msg = " ".join(message.split(" ")[1:])
            if cmd is None and self.fallback:
                cmd = self.commands.get(self.fallback, None)
                extra_msg = message
        elif self.default:
            command = self.default
            cmd = self.commands.get(command, None)
            extra_msg = None

        if cmd:
            if args["user_level"] >= cmd.level:
                return cmd.run(bot=bot, user_id=user_id, channel_id=channel_id, message=extra_msg, whisper=whisper, args=args)
            log.info(f"User {user_id} tried running a sub-command he had no access to ({command}).")

        return None


class FuncAction(BaseAction):
    type = "func"

    def __init__(self, cb):
        self.cb = cb

    def run(self, bot, user_id, channel_id, message, whisper, args):
        try:
            return self.cb(bot=bot, user_id=user_id, message=message, whisper=whisper, args=args)
        except:
            log.exception("Uncaught exception in FuncAction")


class RawFuncAction(BaseAction):
    type = "rawfunc"

    def __init__(self, cb):
        self.cb = cb

    def run(self, bot, user_id, channel_id, message, whisper, args):
        return self.cb(bot=bot, user_id=user_id, message=message, whisper=whisper, args=args)


def get_argument_substitutions(string):
    """
    Returns a list of `Substitution` objects that are found in the passed `string`.
    Will not return multiple `Substitution` objects for the same number.
    This means string "$(1) $(1) $(2)" will only return two Substitutions.
    """

    argument_substitutions = []

    for sub_key in Substitution.argument_substitution_regex.finditer(string):
        needle = sub_key.group(0)
        argument_num = int(sub_key.group(1))

        found = False
        for sub in argument_substitutions:
            if sub.argument == argument_num:
                # We already matched this argument variable
                found = True
                break
        if found:
            continue

        argument_substitutions.append(Substitution(None, needle=needle, argument=argument_num))

    return argument_substitutions


def get_substitution_arguments(sub_key):
    sub_string = sub_key.group(0)
    path = sub_key.group(1)
    argument = sub_key.group(2)
    if argument is not None:
        argument = int(argument[1:])
    key = sub_key.group(3)
    if key is not None:
        key = key[1:]
    matched_filters = sub_key.captures(4)
    matched_filter_arguments = sub_key.captures(5)

    filters = []
    filter_argument_index = 0
    for f in matched_filters:
        f = f[1:]
        filter_arguments = []
        if "(" in f:
            f = f[: -len(matched_filter_arguments[filter_argument_index])]
            filter_arguments = [matched_filter_arguments[filter_argument_index][1:-1]]
            filter_argument_index += 1

        f = SubstitutionFilter(f, filter_arguments)
        filters.append(f)

    if_arguments = sub_key.captures(6)

    return sub_string, path, argument, key, filters, if_arguments


def get_substitutions(string, bot):
    """
    Returns a dictionary of `Substitution` objects thare are found in the passed `string`.
    Will not return multiple `Substitution` objects for the same string.
    This means "You have $(source:points) points xD $(source:points)" only returns one Substitution.
    """

    substitutions = collections.OrderedDict()

    for sub_key in Substitution.substitution_regex.finditer(string):
        sub_string, path, argument, key, filters, if_arguments = get_substitution_arguments(sub_key)

        if sub_string in substitutions:
            # We already matched this variable
            continue

        try:
            if path == "if":
                if if_arguments:
                    if_substitution = IfSubstitution(key, if_arguments, bot)
                    if if_substitution.sub is None:
                        continue
                    sub = Substitution(if_substitution, needle=sub_string, key=key, argument=argument, filters=filters)
                    substitutions[sub_string] = sub
        except:
            log.exception("BabyRage")

    method_mapping = {}
    try:
        # method_mapping["kvi"] = bot.get_kvi_value
        pass
    except AttributeError:
        pass

    for sub_key in Substitution.substitution_regex.finditer(string):
        sub_string, path, argument, key, filters, if_arguments = get_substitution_arguments(sub_key)

        if sub_string in substitutions:
            # We already matched this variable
            continue

        if path in method_mapping:
            sub = Substitution(method_mapping[path], needle=sub_string, key=key, argument=argument, filters=filters)
            substitutions[sub_string] = sub

    return substitutions


def get_urlfetch_substitutions(string, all=False):
    substitutions = {}

    if all:
        r = Substitution.urlfetch_substitution_regex_all
    else:
        r = Substitution.urlfetch_substitution_regex

    for sub_key in r.finditer(string):
        substitutions[sub_key.group(0)] = sub_key.group(1)

    return substitutions


class MessageAction(BaseAction):
    type = "message"

    def __init__(self, response, bot):
        self.response = response
        if bot:
            self.argument_subs = get_argument_substitutions(self.response)
            self.subs = get_substitutions(self.response, bot)
            self.num_urlfetch_subs = len(get_urlfetch_substitutions(self.response, all=True))
        else:
            self.argument_subs = []
            self.subs = {}
            self.num_urlfetch_subs = 0

    @staticmethod
    def get_argument_value(message, index):
        if not message:
            return ""
        msg_parts = message.split(" ")
        try:
            return msg_parts[index]
        except:
            pass
        return ""

    def get_response(self, bot, extra):
        resp = self.response

        resp = apply_substitutions(resp, self.subs, bot, extra)

        if resp is None:
            return None

        for sub in self.argument_subs:
            needle = sub.needle
            value = str(MessageAction.get_argument_value(extra["message"], sub.argument - 1))
            resp = resp.replace(needle, value)
            log.debug(f"Replacing {needle} with {value}")

        return resp

    @staticmethod
    def get_extra_data(user_id, message, args):
        return {"user_id": user_id, "message": message, **args}

    def run(self, bot, user_id, message, whisper, args):
        raise NotImplementedError("Please implement the run method.")


def urlfetch_msg(method, message, num_urlfetch_subs, bot, extra={}, args=[], kwargs={}):
    urlfetch_subs = get_urlfetch_substitutions(message)

    if len(urlfetch_subs) > num_urlfetch_subs:
        log.error(f"HIJACK ATTEMPT {message}")
        return False

    for needle, url in urlfetch_subs.items():
        try:
            headers = {
                "Accept": "text/plain",
                "Accept-Language": "en-US, en;q=0.9, *;q=0.5",
                "User-Agent": bot.user_agent,
            }
            r = requests.get(url, allow_redirects=True, headers=headers)
            r.raise_for_status()
            value = r.text.strip().replace("\n", "").replace("\r", "")[:400]
        except:
            return False
        message = message.replace(needle, value)

    args.append(message)

    method(*args, **kwargs)


class SayAction(MessageAction):
    subtype = "say"

    def run(self, bot, user_id, channel_id, message, whisper, args):
        extra = self.get_extra_data(user_id, message, args)
        resp = self.get_response(bot, extra)

        if not resp:
            return False

        if self.num_urlfetch_subs == 0:
            return bot.say(channel_id, resp)

        return ScheduleManager.execute_now(
            urlfetch_msg,
            args=[],
            kwargs={
                "args": [channel_id],
                "kwargs": {},
                "method": bot.say,
                "bot": bot,
                "extra": extra,
                "message": resp,
                "num_urlfetch_subs": self.num_urlfetch_subs,
            },
        )


class WhisperAction(MessageAction):
    subtype = "whisper"

    def run(self, bot, user_id, channel_id, message, whisper, args):
        extra = self.get_extra_data(user_id, message, args)
        resp = self.get_response(bot, extra)

        if not resp:
            return False

        if self.num_urlfetch_subs == 0:
            return bot.private_message(user_id, resp)

        return ScheduleManager.execute_now(
            urlfetch_msg,
            args=[],
            kwargs={
                "args": [user_id],
                "kwargs": {},
                "method": bot.private_message,
                "bot": bot,
                "extra": extra,
                "message": resp,
                "num_urlfetch_subs": self.num_urlfetch_subs,
            },
        )
