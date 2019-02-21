import discord
from discord.ext import commands
from .utils import checks
import asyncio
import logging
# Casglu Ffurfiwch Ddata
from __main__ import send_cmd_help
from cogs.utils.dataIO import dataIO
import os
import time
import copy
# Dablu
try:
    import tabulate
except ImportError as e:
    raise RuntimeError("Punish requires tabulate. To install it, run `pip3 install tabulate` from the console or "
                       "`[p]debug bot.pip_install('tabulate')` from in Discord.") from e

log = logging.getLogger('red.punish')

try:
    from .mod import CaseMessageNotFound, NoModLogAccess
    ENABLE_MODLOG = True
except ImportError:
    log.warn("Could not import modlog exceptions from mod cog, most likely because mod.py was deleted or Red is out of "
             "date. Modlog integration will be disabled.")
    ENABLE_MODLOG = False

DB_VERSION = 1.48

ACTION_STR = "Timed mute \N{HOURGLASS WITH FLOWING SAND} \N{SPEAKER WITH CANCELLATION STROKE}"
PURGE_MESSAGES = 1  # for cpunish
PATH = 'data/punish/'
JSON = PATH + 'settings.json'

DEFAULT_ROLE_NAME = 'Punished'
DEFAULT_TEXT_OVERWRITE = discord.PermissionOverwrite(send_messages=False, send_tts_messages=False, add_reactions=False)
DEFAULT_VOICE_OVERWRITE = discord.PermissionOverwrite(speak=False)
DEFAULT_TIMEOUT_OVERWRITE = discord.PermissionOverwrite(send_messages=True, read_messages=True)

QUEUE_TIME_CUTOFF = 30

DEFAULT_TIMEOUT = '30m'
DEFAULT_CASE_MIN_LENGTH = '30m'  # only create modlog cases when length is longer than this

UNIT_TABLE = (
    (('weeks', 'wks', 'w'),    60 * 60 * 24 * 7),
    (('days',  'dys', 'd'),    60 * 60 * 24),
    (('hours', 'hrs', 'h'),    60 * 60),
    (('minutes', 'mins', 'm'), 60),
    (('seconds', 'secs', 's'), 1),
)




class BadTimeExpr(Exception):
    pass


def _find_unit(unit):
    for names, length in UNIT_TABLE:
        if any(n.startswith(unit) for n in names):
            return names, length
    raise BadTimeExpr("Invalid unit: %s" % unit)


def _parse_time(time):
    time = time.lower()
    if not time.isdigit():
        time = re.split(r'\s*([\d.]+\s*[^\d\s,;]*)(?:[,;\s]|and)*', time)
        time = sum(map(_timespec_sec, filter(None, time)))
    return int(time)


def _timespec_sec(expr):
    atoms = re.split(r'([\d.]+)\s*([^\d\s]*)', expr)
    atoms = list(filter(None, atoms))

    if len(atoms) > 2:  # This shouldn't ever happen
        raise BadTimeExpr("invalid expression: '%s'" % expr)
    elif len(atoms) == 2:
        names, length = _find_unit(atoms[1])
        if atoms[0].count('.') > 1 or \
                not atoms[0].replace('.', '').isdigit():
            raise BadTimeExpr("Not a number: '%s'" % atoms[0])
    else:
        names, length = _find_unit('seconds')

    try:
        return float(atoms[0]) * length
    except ValueError:
        raise BadTimeExpr("invalid value: '%s'" % atoms[0])


def _generate_timespec(sec: int, short=False, micro=False) -> str:
    timespec = []
    sec = int(sec)
    neg = sec < 0
    sec = abs(sec)

    for names, length in UNIT_TABLE:
        n, sec = divmod(sec, length)

        if n:
            if micro:
                s = '%d%s' % (n, names[2])
            elif short:
                s = '%d%s' % (n, names[1])
            else:
                s = '%d %s' % (n, names[0])

            if n <= 1 and not (micro and names[2] == 's'):
                s = s.rstrip('s')

            timespec.append(s)

    if len(timespec) > 1:
        if micro:
            spec = ''.join(timespec)

        segments = timespec[:-1], timespec[-1:]
        spec = ' and '.join(', '.join(x) for x in segments)
    elif timespec:
        spec = timespec[0]
    else:
        return '0'

    if neg:
        spec += ' ago'

    return spec


def format_list(*items, join='and', delim=', '):
    if len(items) > 1:
        return (' %s ' % join).join((delim.join(items[:-1]), items[-1]))
    elif items:
        return items[0]
    else:
        return ''


def permissions_for_roles(channel, *roles):
    """
    Calculates the effective permissions for a role or combination of roles.
    Naturally, if no roles are given, the default role's permissions are used
    """
    default = channel.server.default_role
    base = discord.Permissions(default.permissions.value)

    # Apply all role values
    for role in roles:
        base.value |= role.permissions.value

    # Server-wide Administrator -> True for everything
    # Bypass all channel-specific overrides
    if base.administrator:
        return discord.Permissions.all()

    role_ids = set(map(lambda r: r.id, roles))
    denies = 0
    allows = 0

    # Apply channel specific role permission overwrites
    for overwrite in channel._permission_overwrites:
        # Handle default role first, if present
        if overwrite.id == default.id:
            base.handle_overwrite(allow=overwrite.allow, deny=overwrite.deny)

        if overwrite.type == 'role' and overwrite.id in role_ids:
            denies |= overwrite.deny
            allows |= overwrite.allow

    base.handle_overwrite(allow=allows, deny=denies)

    # default channels can always be read
    if channel.is_default:
        base.read_messages = True

    # if you can't send a message in a channel then you can't have certain
    # permissions as well
    if not base.send_messages:
        base.send_tts_messages = False
        base.mention_everyone = False
        base.embed_links = False
        base.attach_files = False

    # if you can't read a channel then you have no permissions there
    if not base.read_messages:
        denied = discord.Permissions.all_channel()
        base.value &= ~denied.value

    # text channels do not have voice related permissions
    if channel.type is discord.ChannelType.text:
        denied = discord.Permissions.voice()
        base.value &= ~denied.value

    return base


def overwrite_from_dict(data):
    allow = discord.Permissions(data.get('allow', 0))
    deny = discord.Permissions(data.get('deny', 0))
    return discord.PermissionOverwrite.from_pair(allow, deny)


def overwrite_to_dict(overwrite):
    allow, deny = overwrite.pair()
    return {
        'allow' : allow.value,
        'deny'  : deny.value
    }


def format_permissions(permissions, include_null=False):
    entries = []

    for perm, value in sorted(permissions, key=lambda t: t[0]):
        if value is True:
            symbol = "\N{WHITE HEAVY CHECK MARK}"
        elif value is False:
            symbol = "\N{NO ENTRY SIGN}"
        elif include_null:
            symbol = "\N{RADIO BUTTON}"
        else:
            continue

        entries.append(symbol + ' ' + perm.replace('_', ' ').title().replace("Tts", "TTS"))

    if entries:
        return '\n'.join(entries)
    else:
        return "No permission entries."


def getmname(mid, server):
    member = discord.utils.get(server.members, id=mid)

    if member:
        return str(member)
    else:
        return '(absent user #%s)' % mid


class Punish:
    """
    Put misbehaving users in timeout where they are unable to speak, read, or
    do other things that can be denied using discord permissions. Includes
    auto-setup and more.
    """
    def __init__(self, bot):
        self.bot = bot
        self.json = compat_load(JSON)

        # queue variables
        self.queue = asyncio.PriorityQueue(loop=bot.loop)
        self.queue_lock = asyncio.Lock(loop=bot.loop)
        self.pending = {}
        self.enqueued = set()



        self.task = bot.loop.create_task(self.on_load())

    def __unload(self):
        self.task.cancel()
        self.save()

    def save(self):
        dataIO.save_json(JSON, self.json)

    def can_create_cases(self):
        mod = self.bot.get_cog('Mod')
        if not mod:
            return False

        sig = inspect.signature(mod.new_case)
        return 'force_create' in sig.parameters

    @commands.group(pass_context=True, invoke_without_command=True, no_pm=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def P(self, ctx, user: discord.Member, duration: str = None, *, reason: str = None):
        if ctx.invoked_subcommand:
            return
        elif user:
            await ctx.invoke(self.P_U, user=user, duration=duration, reason=reason)
        else:
            await self.bot.send_cmd_help(ctx)

    @P.command(pass_context=True, no_pm=True, name='start')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_U(self, ctx, user: discord.Member, duration: str = None, *, reason: str = None):
        """
        Puts a user into timeout for a specified time, with optional reason.

        Time specification is any combination of number with the units s,m,h,d,w.
        Example: !punish @idiot 1.1h10m Enough bitching already!
        """

        await self._punish_cmd_common(ctx, user, duration, reason)

    @P.command(pass_context=True, no_pm=True, name='cstart')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_UC(self, ctx, user: discord.Member, duration: str = None, *, reason: str = None):
        """
        Same as [p]punish start, but cleans up the target's last message.
        """

        success = await self._punish_cmd_common(ctx, user, duration, reason, quiet=True)

        if not success:
            return

        def check(m):
            return m.id == ctx.message.id or m.author == user

        try:
            await self.bot.purge_from(ctx.message.channel, limit=PURGE_MESSAGES + 1, check=check)
        except discord.errors.Forbidden:
            await self.bot.say("Punishment set, but I need permissions to manage messages to clean up.")

    @P.command(pass_context=True, no_pm=True, name='list')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_L(self, ctx):
        """
        Shows a table of punished users with time, mod and reason.

        Displays punished users, time remaining, responsible moderator and
        the reason for punishment, if any.
        """

        server = ctx.message.server
        server_id = server.id
        table = []
        now = time.time()
        headers = ['Member', 'Remaining', 'Moderator', 'Reason']
        msg = ''

        # Multiline cell/header support was added in 0.8.0
        if tabulate.__version__ >= '0.8.0':
            headers = [';\n'.join(headers[i::2]) for i in (0, 1)]
        else:
            msg += warning('Compact formatting is only supported with tabulate v0.8.0+ (currently v%s). '
                           'Please update it.\n\n' % tabulate.__version__)

        for member_id, data in self.json.get(server_id, {}).items():
            if not member_id.isdigit():
                continue

            member_name = getmname(member_id, server)
            moderator = getmname(data['by'], server)
            reason = data['reason']
            until = data['until']
            sort = until or float("inf")

            remaining = _generate_timespec(until - now, short=True) if until else 'forever'

            row = [member_name, remaining, moderator, reason or 'No reason set.']

            if tabulate.__version__ >= '0.8.0':
                row[-1] = textwrap.fill(row[-1], 35)
                row = [';\n'.join(row[i::2]) for i in (0, 1)]

            table.append((sort, row))

        if not table:
            await self.bot.say("No users are currently punished.")
            return

        table.sort()
        msg += tabulate.tabulate([k[1] for k in table], headers, tablefmt="grid")

        for page in pagify(msg):
            await self.bot.say(box(page))

    @P.command(pass_context=True, no_pm=True, name='clean')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_C(self, ctx, clean_pending: bool = False):
        """
        Removes absent members from the punished list.

        If run without an argument, it only removes members who are no longer
        present but whose timer has expired. If the argument is 'yes', 1,
        or another trueish value, it will also remove absent members whose
        timers have yet to expire.

        Use this option with care, as removing them will prevent the punished
        role from being re-added if they rejoin before their timer expires.
        """

        count = 0
        now = time.time()
        server = ctx.message.server
        data = self.json.get(server.id, {})

        for mid, mdata in data.copy().items():
            if not mid.isdigit() or server.get_member(mid):
                continue

            elif clean_pending or ((mdata['until'] or 0) < now):
                del(data[mid])
                count += 1

        await self.bot.say('Cleaned %i absent members from the list.' % count)

    @P.command(pass_context=True, no_pm=True, name='clean-bans')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_CB(self, ctx):
        """
        Removes banned members from the punished list.
        """

        count = 0
        now = time.time()
        server = ctx.message.server
        data = self.json.get(server.id, {})

        try:
            bans = await self.bot.get_bans(server)
            ban_ids = {u.id for u in bans}
        except discord.errors.Forbidden:
            await self.bot.say(warning("I need ban permissions to see the list of banned users."))
            return

        for mid, mdata in data.copy().items():
            if not mid.isdigit() or server.get_member(mid):
                continue

            elif mid in ban_ids:
                del(data[mid])
                count += 1

        await self.bot.say('Cleaned %i banned users from the list.' % count)

    @P.command(pass_context=True, no_pm=True, name='warn')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_W(self, ctx, user: discord.Member, *, reason: str = None):
        """
        Warns a user with boilerplate about the rules
        """

        msg = ['Hey %s, ' % user.mention]
        msg.append("you're doing something that might get you muted if you keep "
                   "doing it.")
        if reason:
            msg.append(" Specifically, %s." % reason)

        msg.append("Be sure to review the server rules.")
        await self.bot.say(' '.join(msg))

    @P.command(pass_context=True, no_pm=True, name='end', aliases=['remove'])
    @checks.mod_or_permissions(manage_messages=True)
    async def P_E(self, ctx, user: discord.Member, *, reason: str = None):
        """
        Removes punishment from a user before time has expired

        This is the same as removing the role directly.
        """

        role = await self.get_role(user.server, quiet=True)
        sid = user.server.id
        now = time.time()
        data = self.json.get(sid, {}).get(user.id, {})

        if role and role in user.roles:
            msg = 'Punishment manually ended early by %s.' % ctx.message.author

            original_start = data.get('start')
            original_end = data.get('until')
            remaining = original_end and (original_end - now)

            if remaining:
                msg += ' %s was left' % _generate_timespec(round(remaining))

                if original_start:
                    msg += ' of the original %s.' % _generate_timespec(round(original_end - original_start))
                else:
                    msg += '.'

            if reason:
                msg += '\n\nReason for ending early: ' + reason

            if data.get('reason'):
                msg += '\n\nOriginal reason was: ' + data['reason']

            if not await self._unpunish(user, msg, update=True):
                msg += '\n\n(failed to send punishment end notification DM)'

            await self.bot.say(msg)
        elif data:  # This shouldn't happen, but just in case
            now = time.time()
            until = data.get('until')
            remaining = until and _generate_timespec(round(until - now)) or 'forever'

            data_fmt = '\n'.join([
                "**Reason:** %s" % (data.get('reason') or 'no reason set'),
                "**Time remaining:** %s" % remaining,
                "**Moderator**: %s" % (user.server.get_member(data.get('by')) or 'Missing ID#%s' % data.get('by'))
            ])
            self.json[sid].pop(user.id, None)
            self.save()
            await self.bot.say("That user doesn't have the %s role, but they still have a data entry. I removed it, "
                               "but in case it's needed, this is what was there:\n\n%s" % (role.name, data_fmt))
        elif role:
            await self.bot.say("That user doesn't have the %s role." % role.name)
        else:
            await self.bot.say("The punish role couldn't be found in this server.")

    @P.command(pass_context=True, no_pm=True, name='reason')
    @checks.mod_or_permissions(manage_messages=True)
    async def P_R(self, ctx, user: discord.Member, *, reason: str = None):
        """
        Updates the reason for a punishment, including the modlog if a case exists.
        """
        server = ctx.message.server
        data = self.json.get(server.id, {}).get(user.id, {})

        if not data:
            await self.bot.say("That user doesn't have an active punishment entry. To update modlog "
                               "cases manually, use the `%sreason` command." % ctx.prefix)
            return

        data['reason'] = reason
        self.save()
        if reason:
            msg = 'Reason updated.'
        else:
            msg = 'Reason cleared'

        caseno = data.get('caseno')
        mod = self.bot.get_cog('Mod')

        if mod and caseno and ENABLE_MODLOG:
            moderator = ctx.message.author
            case_error = None

            try:
                if moderator.id != data.get('by') and not mod.is_admin_or_superior(moderator):
                    moderator = server.get_member(data.get('by')) or server.me  # fallback gracefully

                await mod.update_case(server, case=caseno, reason=reason, mod=moderator)
            except CaseMessageNotFound:
                case_error = 'the case message could not be found'
            except NoModLogAccess:
                case_error = 'I do not have access to the modlog channel'
            except Exception:
                pass

            if case_error:
                msg += '\n\n' + warning('There was an error updating the modlog case: %s.' % case_error)

        await self.bot.say(msg)

    @commands.group(pass_context=True, invoke_without_command=True, no_pm=True)
    @checks.admin_or_permissions(administrator=True)
    async def PS(self, ctx):
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @PS.command(pass_context=True, no_pm=True, name='setup')
    async def PS_setup(self, ctx):
        """
        (Re)configures the punish role and channel overrides
        """
        server = ctx.message.server
        default_name = DEFAULT_ROLE_NAME
        role_id = self.json.get(server.id, {}).get('ROLE_ID')

        if role_id:
            role = discord.utils.get(server.roles, id=role_id)
        else:
            role = discord.utils.get(server.roles, name=default_name)

        perms = server.me.server_permissions
        if not perms.manage_roles and perms.manage_channels:
            await self.bot.say("I need the Manage Roles and Manage Channels permissions for that command to work.")
            return

        if not role:
            msg = "The %s role doesn't exist; Creating it now... " % default_name

            msgobj = await self.bot.say(msg)

            perms = discord.Permissions.none()
            role = await self.bot.create_role(server, name=default_name, permissions=perms)
        else:
            msgobj = await self.bot.say('%s role exists... ' % role.name)

        if role.position != (server.me.top_role.position - 1):
            if role < server.me.top_role:
                msgobj = await self.bot.edit_message(msgobj, msgobj.content + 'moving role to higher position... ')
                await self.bot.move_role(server, role, server.me.top_role.position - 1)
            else:
                await self.bot.edit_message(msgobj, msgobj.content + 'role is too high to manage.'
                                            ' Please move it to below my highest role.')
                return

        msgobj = await self.bot.edit_message(msgobj, msgobj.content + '(re)configuring channels... ')

        for channel in server.channels:
            await self.setup_channel(channel, role)

        await self.bot.edit_message(msgobj, msgobj.content + 'done.')

        if role and role.id != role_id:
            if server.id not in self.json:
                self.json[server.id] = {}
            self.json[server.id]['ROLE_ID'] = role.id
            self.save()

    @PS.command(pass_context=True, no_pm=True, name='channel')
    async def PS_C(self, ctx, channel: discord.Channel = None):
        """
        Sets or shows the punishment "timeout" channel.

        This channel has special settings to allow punished users to discuss their
        infraction(s) with moderators.

        If there is a role deny on the channel for the punish role, it is
        automatically set to allow. If the default permissions don't allow the
        punished role to see or speak in it, an overwrite is created to allow
        them to do so.
        """
        server = ctx.message.server
        current = self.json.get(server.id, {}).get('CHANNEL_ID')
        current = current and server.get_channel(current)

        if channel is None:
            if not current:
                await self.bot.say("No timeout channel has been set.")
            else:
                await self.bot.say("The timeout channel is currently %s." % current.mention)
        else:
            if server.id not in self.json:
                self.json[server.id] = {}
            elif current == channel:
                await self.bot.say("The timeout channel is already %s. If you need to repair its permissions, use "
                                   "`%sPS setup`." % (current.mention, ctx.prefix))
                return

            self.json[server.id]['CHANNEL_ID'] = channel.id
            self.save()

            role = await self.get_role(server, create=True)
            update_msg = '{} to the %s role' % role
            grants = []
            denies = []
            perms = permissions_for_roles(channel, role)
            overwrite = channel.overwrites_for(role) or discord.PermissionOverwrite()

            for perm, value in DEFAULT_TIMEOUT_OVERWRITE:
                if value is None:
                    continue

                if getattr(perms, perm) != value:
                    setattr(overwrite, perm, value)
                    name = perm.replace('_', ' ').title().replace("Tts", "TTS")

                    if value:
                        grants.append(name)
                    else:
                        denies.append(name)

            # Any changes made? Apply them.
            if grants or denies:
                grants = grants and ('grant ' + format_list(*grants))
                denies = denies and ('deny ' + format_list(*denies))
                to_join = [x for x in (grants, denies) if x]
                update_msg = update_msg.format(format_list(*to_join))

                if current and current.id != channel.id:
                    if current.permissions_for(server.me).manage_roles:
                        msg = info("Resetting permissions in the old channel (%s) to the default...")
                    else:
                        msg = error("I don't have permissions to reset permissions in the old channel (%s)")

                    await self.bot.say(msg % current.mention)
                    await self.setup_channel(current, role)

                if channel.permissions_for(server.me).manage_roles:
                    await self.bot.say(info('Updating permissions in %s to %s...' % (channel.mention, update_msg)))
                    await self.bot.edit_channel_permissions(channel, role, overwrite)
                else:
                    await self.bot.say(error("I don't have permissions to %s." % update_msg))

            await self.bot.say("Timeout channel set to %s." % channel.mention)

    @.command(pass_context=True, no_pm=True, name='clear-channel')
    async def PS_CC(self, ctx):
        """
        Clears the timeout channel and resets its permissions
        """
        server = ctx.message.server
        current = self.json.get(server.id, {}).get('CHANNEL_ID')
        current = current and server.get_channel(current)

        if current:
            msg = None
            self.json[server.id]['CHANNEL_ID'] = None
            self.save()

            if current.permissions_for(server.me).manage_roles:
                role = await self.get_role(server, quiet=True)
                await self.setup_channel(current, role)
                msg = ' and its permissions reset'
            else:
                msg = ", but I don't have permissions to reset its permissions."

            await self.bot.say("Timeout channel has been cleared%s." % msg)
        else:
            await self.bot.say("No timeout channel has been set yet.")

    @PS.command(pass_context=True, allow_dm=False, name='case-min')
    async def PS_CM(self, ctx, *, timespec: str = None):
        """
        Set/disable or display the minimum punishment case duration

        If the punishment duration is less than this value, a case will not be created.
        Specify 'disable' to turn off case creation altogether.
        """
        server = ctx.message.server
        current = self.json[server.id].get('CASE_MIN_LENGTH', _parse_time(DEFAULT_CASE_MIN_LENGTH))

        if not timespec:
            if current:
                await self.bot.say('Punishments longer than %s will create cases.' % _generate_timespec(current))
            else:
                await self.bot.say("Punishment case creation is disabled.")
        else:
            if timespec.strip('\'"').lower() == 'disable':
                value = None
            else:
                try:
                    value = _parse_time(timespec)
                except BadTimeExpr as e:
                    await self.bot.say(error(e.args[0]))
                    return

            if server.id not in self.json:
                self.json[server.id] = {}

            self.json[server.id]['CASE_MIN_LENGTH'] = value
            self.save()

    @PS.command(pass_context=True, no_pm=True, name='overrides')
    async def PS_O(self, ctx, *, channel: discord.Channel = None):
        """
        Copy or display the punish role overrides

        If a channel is specified, the allow/deny settings for it are saved
        and applied to new channels when they are created. To apply the new
        settings to existing channels, use [p]PS setup.

        An important caveat: voice channel and text channel overrides are
        configured separately! To set the overrides for a channel type,
        specify the name of or mention a channel of that type.
        """

        server = ctx.message.server
        settings = self.json.get(server.id, {})
        role = await self.get_role(server, quiet=True)
        timeout_channel_id = settings.get('CHANNEL_ID')
        confirm_msg = None

        if not role:
            await self.bot.say(error("Punish role has not been created yet. Run `%sPS setup` first."
                                     % ctx.prefix))
            return

        if channel:
            overwrite = channel.overwrites_for(role)
            if channel.id == timeout_channel_id:
                confirm_msg = "Are you sure you want to copy overrides from the timeout channel?"
            elif overwrite is None:
                overwrite = discord.PermissionOverwrite()
                confirm_msg = "Are you sure you want to copy blank (no permissions set) overrides?"

            if channel.type is discord.ChannelType.text:
                key = 'text'
            elif channel.type is discord.ChannelType.voice:
                key = 'voice'
            else:
                await self.bot.say(error("Unknown channel type!"))
                return

            if confirm_msg:
                await self.bot.say(warning(confirm_msg + '(reply `yes` within 30s to confirm)'))
                reply = await self.bot.wait_for_message(channel=ctx.message.channel, author=ctx.message.author,
                                                        timeout=30)

                if reply is None:
                    await self.bot.say('Timed out waiting for a response.')
                    return
                elif reply.content.strip(' `"\'').lower() != 'yes':
                    await self.bot.say('Commmand cancelled.')
                    return

            self.json[server.id][key.upper() + '_OVERWRITE'] = overwrite_to_dict(overwrite)
            self.save()
            await self.bot.say("{} channel overrides set to:\n".format(key.title()) +
                               format_permissions(overwrite) +
                               "\n\nRun `%sPS setup` to apply them to all channels." % ctx.prefix)

        else:
            msg = []
            for key, default in [('text', DEFAULT_TEXT_OVERWRITE), ('voice', DEFAULT_VOICE_OVERWRITE)]:
                data = settings.get(key.upper() + '_OVERWRITE')
                title = '%s permission overrides:' % key.title()

                if not data:
                    data = overwrite_to_dict(default)
                    title = title[:-1] + ' (defaults):'

                msg.append(bold(title) + '\n' + format_permissions(overwrite_from_dict(data)))

            await self.bot.say('\n\n'.join(msg))

    @PS.command(pass_context=True, no_pm=True, name='reset-overrides')
    async def PS_RO(self, ctx, channel_type: str = 'both'):
        """
        Resets the punish role overrides for text, voice or both (default)

        This command exists in case you want to restore the default settings
        for newly created channels.
        """

        settings = self.json.get(ctx.message.server.id, {})
        channel_type = channel_type.strip('`"\' ').lower()

        msg = []
        for key, default in [('text', DEFAULT_TEXT_OVERWRITE), ('voice', DEFAULT_VOICE_OVERWRITE)]:
            if channel_type not in ['both', key]:
                continue

            settings.pop(key.upper() + '_OVERWRITE', None)
            title = '%s permission overrides reset to:' % key.title()
            msg.append(bold(title) + '\n' + format_permissions(default))

        if not msg:
            await self.bot.say("Invalid channel type. Use `text`, `voice`, or `both` (the default, if not specified)")
            return

        msg.append("Run `%sPS setup` to apply them to all channels." % ctx.prefix)

        self.save()
        await self.bot.say('\n\n'.join(msg))

    async def get_role(self, server, quiet=False, create=False):
        default_name = DEFAULT_ROLE_NAME
        role_id = self.json.get(server.id, {}).get('ROLE_ID')

        if role_id:
            role = discord.utils.get(server.roles, id=role_id)
        else:
            role = discord.utils.get(server.roles, name=default_name)

        if create and not role:
            perms = server.me.server_permissions
            if not perms.manage_roles and perms.manage_channels:
                await self.bot.say("The Manage Roles and Manage Channels permissions are required to use this command.")
                return

            else:
                msg = "The %s role doesn't exist; Creating it now..." % default_name

                if not quiet:
                    msgobj = await self.bot.reply(msg)

                log.debug('Creating punish role in %s' % server.name)
                perms = discord.Permissions.none()
                role = await self.bot.create_role(server, name=default_name, permissions=perms)
                await self.bot.move_role(server, role, server.me.top_role.position - 1)

                if not quiet:
                    msgobj = await self.bot.edit_message(msgobj, msgobj.content + 'configuring channels... ')

                for channel in server.channels:
                    await self.setup_channel(channel, role)

                if not quiet:
                    await self.bot.edit_message(msgobj, msgobj.content + 'done.')

        if role and role.id != role_id:
            if server.id not in self.json:
                self.json[server.id] = {}

            self.json[server.id]['ROLE_ID'] = role.id
            self.save()

        return role

    # Legacy command stubs

    @commands.command(pass_context=True, no_pm=True)
    async def legacy_lspunish(self, ctx):
        await self.bot.say("This command is deprecated; use `%spunish list` instead.\n\n"
                           "This notice will be removed in a future release." % ctx.prefix)

    @commands.command(pass_context=True, no_pm=True)
    async def legacy_cpunish(self, ctx):
        await self.bot.say("This command is deprecated; use `%spunish cstart <member> [duration] [reason ...]` "
                           "instead.\n\nThis notice will be removed in a future release." % ctx.prefix)

    @commands.command(pass_context=True, no_pm=True, name='punish-clean')
    async def legacy_punish_clean(self, ctx):
        await self.bot.say("This command is deprecated; use `%spunish clean` instead.\n\n"
                           "This notice will be removed in a future release." % ctx.prefix)

    @commands.command(pass_context=True, no_pm=True)
    async def legacy_pwarn(self, ctx):
        await self.bot.say("This command is deprecated; use `%spunish warn` instead.\n\n"
                           "This notice will be removed in a future release." % ctx.prefix)

    @commands.command(pass_context=True, no_pm=True)
    async def legacy_fixpunish(self, ctx):
        await self.bot.say("This command is deprecated; use `%sPS setup` instead.\n\n"
                           "This notice will be removed in a future release." % ctx.prefix)

    async def setup_channel(self, channel, role):
        settings = self.json.get(channel.server.id, {})
        timeout_channel_id = settings.get('CHANNEL_ID')

        if channel.id == timeout_channel_id:
            # maybe this will be used later:
            # config = settings.get('TIMEOUT_OVERWRITE')
            config = None
            defaults = DEFAULT_TIMEOUT_OVERWRITE
        elif channel.type is discord.ChannelType.voice:
            config = settings.get('VOICE_OVERWRITE')
            defaults = DEFAULT_VOICE_OVERWRITE
        else:
            config = settings.get('TEXT_OVERWRITE')
            defaults = DEFAULT_TEXT_OVERWRITE

        if config:
            perms = overwrite_from_dict(config)
        else:
            perms = defaults

        await self.bot.edit_channel_permissions(channel, role, overwrite=perms)

    async def on_load(self):
        await self.bot.wait_until_ready()

        for serverid, members in self.json.copy().items():
            server = self.bot.get_server(serverid)

            # Bot is no longer in the server
            if not server:
                del(self.json[serverid])
                continue

            me = server.me
            role = await self.get_role(server, quiet=True, create=True)

            if not role:
                log.error("Needed to create punish role in %s, but couldn't." % server.name)
                continue

            for member_id, data in members.copy().items():
                if not member_id.isdigit():
                    continue

                until = data['until']
                member = server.get_member(member_id)

                if until and (until - time.time()) < 0:
                    if member:
                        reason = 'Punishment removal overdue, maybe the bot was offline. '

                        if self.json[server.id][member_id]['reason']:
                            reason += self.json[server.id][member_id]['reason']

                        await self._unpunish(member, reason)
                    else:  # member disappeared
                        del(self.json[server.id][member_id])

                elif member:
                    if role not in member.roles:
                        if role >= me.top_role:
                            log.error("Needed to re-add punish role to %s in %s, but couldn't." % (member, server.name))
                            continue

                        await self.bot.add_roles(member, role)

                    if until:
                        await self.schedule_unpunish(until, member)

        self.save()

        while True:
            try:
                async with self.queue_lock:
                    while await self.process_queue_event():
                        pass

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception:
                pass

        log.debug('queue manager dying')

        while not self.queue.empty():
            self.queue.get_nowait()

        for fut in self.pending.values():
            fut.cancel()

    async def cancel_queue_event(self, *args) -> bool:
        if args in self.pending:
            self.pending.pop(args).cancel()
            return True
        else:
            events = []
            removed = None

            async with self.queue_lock:
                while not self.queue.empty():
                    item = self.queue.get_nowait()

                    if args == item[1:]:
                        removed = item
                        break
                    else:
                        events.append(item)

                for item in events:
                    self.queue.put_nowait(item)

            return removed is not None

    async def put_queue_event(self, run_at : float, *args):
        diff = run_at - time.time()

        if args in self.enqueued:
            return False

        self.enqueued.add(args)

        if diff < 0:
            self.execute_queue_event(*args)
        elif run_at - time.time() < QUEUE_TIME_CUTOFF:
            self.pending[args] = self.bot.loop.call_later(diff, self.execute_queue_event, *args)
        else:
            await self.queue.put((run_at, *args))

    async def process_queue_event(self):
        if self.queue.empty():
            return False

        now = time.time()
        item = await self.queue.get()
        next_time, *args = item

        diff = next_time - now

        if diff < 0:
            if self.execute_queue_event(*args):
                return
        elif diff < QUEUE_TIME_CUTOFF:
            self.pending[args] = self.bot.loop.call_later(diff, self.execute_queue_event, *args)
            return True

        await self.queue.put(item)
        return False

    def execute_queue_event(self, *args) -> bool:
        self.enqueued.discard(args)

        try:
            return self.execute_unpunish(*args)
        except Exception:
            log.exception("failed to execute scheduled event")

    async def _punish_cmd_common(self, ctx, member, duration, reason, quiet=False):
        server = ctx.message.server
        using_default = False
        updating_case = False
        case_error = None
        mod = self.bot.get_cog('Mod')

        if server.id not in self.json:
            self.json[server.id] = {}

        current = self.json[server.id].get(member.id, {})
        reason = reason or current.get('reason')  # don't clear if not given
        hierarchy_allowed = ctx.message.author.top_role > member.top_role
        case_min_length = self.json[server.id].get('CASE_MIN_LENGTH', _parse_time(DEFAULT_CASE_MIN_LENGTH))

        if mod:
            hierarchy_allowed = mod.is_allowed_by_hierarchy(server, ctx.message.author, member)

        if not hierarchy_allowed:
            await self.bot.say('Permission denied due to role hierarchy.')
            return
        elif member == server.me:
            await self.bot.say("You can't punish the bot.")
            return

        if duration and duration.lower() in ['forever', 'inf', 'infinite']:
            duration = None
        else:
            if not duration:
                using_default = True
                duration = DEFAULT_TIMEOUT

            try:
                duration = _parse_time(duration)
                if duration < 1:
                    await self.bot.say("Duration must be 1 second or longer.")
                    return False
            except BadTimeExpr as e:
                await self.bot.say("Error parsing duration: %s." % e.args)
                return False

        role = await self.get_role(server, quiet=quiet, create=True)
        if role is None:
            return

        if role >= server.me.top_role:
            await self.bot.say('The %s role is too high for me to manage.' % role)
            return

        # Call time() after getting the role due to potential creation delay
        now = time.time()
        until = (now + duration + 0.5) if duration else None
        duration_ok = (case_min_length is not None) and ((duration is None) or duration >= case_min_length)

        if mod and self.can_create_cases() and duration_ok and ENABLE_MODLOG:
            mod_until = until and datetime.utcfromtimestamp(until)

            try:
                if current:
                    case_number = current.get('caseno')
                    moderator = ctx.message.author
                    updating_case = True

                    # update_case does ownership checks, we need to cheat them in case the
                    # command author doesn't qualify to edit a case
                    if moderator.id != current.get('by') and not mod.is_admin_or_superior(moderator):
                        moderator = server.get_member(current.get('by')) or server.me  # fallback gracefully

                    await mod.update_case(server, case=case_number, reason=reason, mod=moderator,
                                          until=mod_until and mod_until.timestamp() or False)
                else:
                    case_number = await mod.new_case(server, action=ACTION_STR, mod=ctx.message.author,
                                                     user=member, reason=reason, until=mod_until,
                                                     force_create=True)
            except Exception as e:
                case_error = e
        else:
            case_number = None

        subject = 'the %s role' % role.name

        if member.id in self.json[server.id]:
            if role in member.roles:
                msg = '{0} already had the {1.name} role; resetting their timer.'
            else:
                msg = '{0} is missing the {1.name} role for some reason. I added it and reset their timer.'
        elif role in member.roles:
            msg = '{0} already had the {1.name} role, but had no timer; setting it now.'
        else:
            msg = 'Applied the {1.name} role to {0}.'
            subject = 'it'

        msg = msg.format(member, role)

        if duration:
            timespec = _generate_timespec(duration)

            if using_default:
                timespec += ' (the default)'

            msg += ' I will remove %s in %s.' % (subject, timespec)

        if duration_ok and not (self.can_create_cases() and ENABLE_MODLOG):
            if mod:
                msg += '\n\n' + warning('If you can, please update the bot so I can create modlog cases.')
            else:
                pass  # msg += '\n\nI cannot create modlog cases if the `mod` cog is not loaded.'
        elif case_error and ENABLE_MODLOG:
            if isinstance(case_error, CaseMessageNotFound):
                case_error = 'the case message could not be found'
            elif isinstance(case_error, NoModLogAccess):
                case_error = 'I do not have access to the modlog channel'
            else:
                case_error = None

            if case_error:
                verb = 'updating' if updating_case else 'creating'
                msg += '\n\n' + warning('There was an error %s the modlog case: %s.' % (verb, case_error))
        elif case_number:
            verb = 'updated' if updating_case else 'created'
            msg += ' I also %s case #%i in the modlog.' % (verb, case_number)

        voice_overwrite = self.json[server.id].get('VOICE_OVERWRITE')

        if voice_overwrite:
            voice_overwrite = overwrite_from_dict(voice_overwrite)
        else:
            voice_overwrite = DEFAULT_VOICE_OVERWRITE

        overwrite_denies_speak = (voice_overwrite.speak is False) or (voice_overwrite.connect is False)

        self.json[server.id][member.id] = {
            'start'  : current.get('start') or now,  # don't override start time if updating
            'until'  : until,
            'by'     : current.get('by') or ctx.message.author.id,  # don't override original moderator
            'reason' : reason,
            'unmute' : overwrite_denies_speak and not member.voice.mute,
            'caseno' : case_number
        }

        await self.bot.add_roles(member, role)

        if member.voice_channel and overwrite_denies_speak:
            await self.bot.server_voice_state(member, mute=True)

        self.save()

        # schedule callback for role removal
        if until:
            await self.schedule_unpunish(until, member)

        if not quiet:
            await self.bot.say(msg)

        return True

    # Functions related to unpunishing

    async def schedule_unpunish(self, until, member):
        """
        Schedules role removal, canceling and removing existing tasks if present
        """

        await self.put_queue_event(until, member.server.id, member.id)

    def execute_unpunish(self, server_id, member_id) -> bool:
        server = self.bot.get_server(server_id)

        if not server:
            return False

        member = server.get_member(member_id)

        if member:
            self.bot.loop.create_task(self._unpunish(member))
            return True
        else:
            self.bot.loop.create_task(self.bot.request_offline_members(server))
            return False

    async def _unpunish(self, member, reason=None, remove_role=True, update=False, moderator=None, quiet=False) -> bool:
        """
        Remove punish role, delete record and task handle
        """
        server = member.server
        role = await self.get_role(server, quiet=True)

        if role:
            data = self.json.get(member.server.id, {})
            member_data = data.get(member.id, {})
            caseno = member_data.get('caseno')
            mod = self.bot.get_cog('Mod')

            # Has to be done first to prevent triggering listeners
            self._unpunish_data(member)
            await self.cancel_queue_event(member.server.id, member.id)

            if remove_role:
                await self.bot.remove_roles(member, role)

            if update and caseno and mod:
                until = member_data.get('until') or False

                if until:
                    until = datetime.utcfromtimestamp(until).timestamp()

                if moderator and moderator.id != member_data.get('by') and not mod.is_admin_or_superior(moderator):
                    moderator = None

                # fallback gracefully
                moderator = moderator or server.get_member(member_data.get('by')) or server.me

                try:
                    await mod.update_case(server, case=caseno, reason=reason, mod=moderator, until=until)
                except Exception:
                    pass

            if member_data.get('unmute', False):
                if member.voice_channel:
                    await self.bot.server_voice_state(member, mute=False)
                else:
                    if 'PENDING_UNMUTE' not in data:
                        data['PENDING_UNMUTE'] = []

                    unmute_list = data['PENDING_UNMUTE']

                    if member.id not in unmute_list:
                        unmute_list.append(member.id)
                    self.save()

            if quiet:
                return True

            msg = 'Your punishment in %s has ended.' % member.server.name

            if reason:
                msg += "\nReason: %s" % reason

            try:
                await self.bot.send_message(member, msg)
                return True
            except Exception:
                return False

    def _unpunish_data(self, member):
        """Removes punish data entry and cancels any present callback"""
        sid = member.server.id

        if member.id in self.json.get(sid, {}):
            del(self.json[member.server.id][member.id])
            self.save()

    # Listeners

    async def on_channel_create(self, channel):
        """Run when new channels are created and set up role permissions"""
        if channel.is_private:
            return

        role = await self.get_role(channel.server, quiet=True)
        if not role:
            return

        await self.setup_channel(channel, role)

    async def on_member_update(self, before, after):
        """Remove scheduled unpunish when manually removed"""
        sid = before.server.id
        data = self.json.get(sid, {})
        member_data = data.get(before.id)

        if member_data is None:
            return

        role = await self.get_role(before.server, quiet=True)
        if role and role in before.roles and role not in after.roles:
            msg = 'Punishment manually ended early by a moderator/admin.'
            if member_data['reason']:
                msg += '\nReason was: ' + member_data['reason']

            await self._unpunish(after, msg, remove_role=False, update=True)

    async def on_member_join(self, member):
        """Restore punishment if punished user leaves/rejoins"""
        sid = member.server.id
        role = await self.get_role(member.server, quiet=True)
        data = self.json.get(sid, {}).get(member.id)
        if not role or data is None:
            return

        until = data['until']
        duration = until - time.time()
        if duration > 0:
            await self.bot.add_roles(member, role)
            await self.schedule_unpunish(until, member)

    async def on_voice_state_update(self, before, after):
        data = self.json.get(before.server.id, {})
        member_data = data.get(before.id, {})
        unmute_list = data.get('PENDING_UNMUTE', [])

        if not after.voice_channel:
            return

        if member_data and not after.voice.mute:
            await self.bot.server_voice_state(after, mute=True)

        elif before.id in unmute_list:
            await self.bot.server_voice_state(after, mute=False)
            while before.id in unmute_list:
                unmute_list.remove(before.id)
            self.save()

    async def on_member_ban(self, member):
        """Remove punishment record when member is banned."""
        sid = member.server.id
        data = self.json.get(sid, {})
        member_data = data.get(member.id)

        if member_data is None:
            return

        msg = "Punishment ended early due to ban."

        if member_data.get('reason'):
            msg += '\n\nOriginal reason was: ' + member_data['reason']

        await self._unpunish(member, msg, remove_role=False, update=True, quiet=True)




def compat_load(path):
    data = dataIO.load_json(path)
    for server, punishments in data.items():
        for user, pdata in punishments.items():
            if not user.isdigit():
                continue

            # read Kownlin json
            by = pdata.pop('givenby', None)
            by = by if by else pdata.pop('by', None)
            pdata['by'] = by
            pdata['until'] = pdata.pop('until', None)
            pdata['reason'] = pdata.pop('reason', None)

    return data


def check_folder():
    if not os.path.exists(PATH):
        log.debug('Creating folder: data/punish')
        os.makedirs(PATH)


def check_file():
    if not dataIO.is_valid_json(JSON):
        print('Creating empty %s' % JSON)
        dataIO.save_json(JSON, {})


def setup(bot):
    check_folder()
    check_file()
    bot.add_cog(Punish(bot))
