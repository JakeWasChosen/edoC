# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
#  Copyright (c) 2021. Jason Cameron                                                               +
#  All rights reserved.                                                                            +
#  This file is part of the edoC discord bot project ,                                             +
#  and is released under the "MIT License Agreement". Please see the LICENSE                       +
#  file that should have been included as part of this package.                                    +
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
from typing import Optional

import discord
from discord.ext import commands

from utils import checks, cache
from utils.views import edoCPages


class Config(commands.Cog):
    """Handles the bot's configuration system.
    This is how you disable or enable certain commands
    for your server or block certain channels or members.
    """

    def __init__(self, bot):
        self.bot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{GEAR}\ufe0f')

    @cache.cache(strategy=cache.Strategy.lru, maxsize=1024, ignore_kwargs=True)
    async def is_plonked(self, guild_id, member_id, channel=None, *, connection=None, check_bypass=True):
        if member_id in self.bot.blacklist or guild_id in self.bot.blacklist:
            return True

        if check_bypass:
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                member = await self.bot.get_or_fetch_member(guild, member_id)
                if member is not None and member.guild_permissions.manage_guild:
                    return False

        connection = connection or self.bot.pool

        if channel is None:
            query = "SELECT 1 FROM plonks WHERE guild_id=$1 AND entity_id=$2;"
            row = await connection.fetchrow(query, guild_id, member_id)
        else:
            if isinstance(channel, discord.Thread):
                query = "SELECT 1 FROM plonks WHERE guild_id=$1 AND entity_id IN ($2, $3, $4);"
                row = await connection.fetchrow(query, guild_id, member_id, channel.id, channel.parent_id)
            else:
                query = "SELECT 1 FROM plonks WHERE guild_id=$1 AND entity_id IN ($2, $3);"
                row = await connection.fetchrow(query, guild_id, member_id, channel.id)

        return row is not None

    async def bot_check_once(self, ctx):
        if ctx.guild is None:
            return True

        is_owner = await ctx.bot.is_owner(ctx.author)
        if is_owner:
            return True

        # see if they can bypass:
        if isinstance(ctx.author, discord.Member):
            bypass = ctx.author.guild_permissions.manage_guild
            if bypass:
                return True

        # check if we're plonked
        is_plonked = await self.is_plonked(ctx.guild.id, ctx.author.id, channel=ctx.channel,
                                                                        connection=ctx.db, check_bypass=False)

        return not is_plonked

    @cache.cache()
    async def get_command_permissions(self, guild_id, *, connection=None):
        connection = connection or self.bot.pool
        query = "SELECT name, channel_id, whitelist FROM command_config WHERE guild_id=$1;"

        records = await connection.fetch(query, guild_id)
        return ResolvedCommandPermissions(guild_id, records)

    async def bot_check(self, ctx):
        if ctx.guild is None:
            return True

        is_owner = await ctx.bot.is_owner(ctx.author)
        if is_owner:
            return True

        resolved = await self.get_command_permissions(ctx.guild.id, connection=ctx.db)
        return not resolved.is_blocked(ctx)

    async def _bulk_ignore_entries(self, ctx, entries):
        async with ctx.acquire():
            async with ctx.db.transaction():
                query = "SELECT entity_id FROM plonks WHERE guild_id=$1;"
                records = await ctx.db.fetch(query, ctx.guild.id)

                # we do not want to insert duplicates
                current_plonks = {r[0] for r in records}
                guild_id = ctx.guild.id
                to_insert = [(guild_id, e.id) for e in entries if e.id not in current_plonks]

                # do a bulk COPY
                await ctx.db.copy_records_to_table('plonks', columns=('guild_id', 'entity_id'), records=to_insert)

                # invalidate the cache for this guild
                self.is_plonked.invalidate_containing(f'{ctx.guild.id!r}:')

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(error)

    @commands.group()
    async def config(self, ctx):
        """Handles the server or channel permission configuration for the bot."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help('config')

    @config.group(invoke_without_command=True, aliases=['plonk'])
    @checks.is_mod()
    async def ignore(self, ctx, *entities: ChannelOrMember):
        """Ignores text channels or members from using the bot.
        If no channel or member is specified, the current channel is ignored.
        Users with Manage Server can still use the bot, regardless of ignore
        status.
        To use this command you must have Manage Server permissions.
        """

        if len(entities) == 0:
            # shortcut for a single insert
            query = "INSERT INTO plonks (guild_id, entity_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;"
            await ctx.db.execute(query, ctx.guild.id, ctx.channel.id)

            # invalidate the cache for this guild
            self.is_plonked.invalidate_containing(f'{ctx.guild.id!r}:')
        else:
            await self._bulk_ignore_entries(ctx, entities)

        await ctx.send(ctx.tick(True))

    @ignore.command(name='list')
    @checks.is_mod()
    @commands.cooldown(2.0, 60.0, commands.BucketType.guild)
    async def ignore_list(self, ctx):
        """Tells you what channels or members are currently ignored in this server.
        To use this command you must have Manage Server permissions.
        """

        query = "SELECT entity_id FROM plonks WHERE guild_id=$1;"

        guild = ctx.guild
        records = await ctx.db.fetch(query, guild.id)

        if len(records) == 0:
            return await ctx.send('I am not ignoring anything here.')

        await ctx.release()

        source = PlonkedPageSource(self.bot, guild, records)
        pages = edoCPages(source, ctx=ctx)
        await pages.start()

    @ignore.command(name='all')
    @checks.is_mod()
    async def _all(self, ctx):
        """Ignores every channel in the server from being processed.
        This works by adding every channel that the server currently has into
        the ignore list. If more channels are added then they will have to be
        ignored by using the ignore command.
        To use this command you must have Manage Server permissions.
        """
        await self._bulk_ignore_entries(ctx, ctx.guild.text_channels)
        await ctx.send('Successfully blocking all channels here.')

    @ignore.command(name='clear')
    @checks.is_mod()
    async def ignore_clear(self, ctx):
        """Clears all the currently set ignores.
        To use this command you must have Manage Server permissions.
        """

        query = "DELETE FROM plonks WHERE guild_id=$1;"
        await ctx.db.execute(query, ctx.guild.id)
        self.is_plonked.invalidate_containing(f'{ctx.guild.id!r}:')
        await ctx.send('Successfully cleared all ignores.')

    @config.group(pass_context=True, invoke_without_command=True, aliases=['unplonk'])
    @checks.is_mod()
    async def unignore(self, ctx, *entities: ChannelOrMember):
        """Allows channels or members to use the bot again.
        If nothing is specified, it unignores the current channel.
        To use this command you must have the Manage Server permission.
        """

        if len(entities) == 0:
            query = "DELETE FROM plonks WHERE guild_id=$1 AND entity_id=$2;"
            await ctx.db.execute(query, ctx.guild.id, ctx.channel.id)
        else:
            query = "DELETE FROM plonks WHERE guild_id=$1 AND entity_id = ANY($2::bigint[]);"
            entities = [c.id for c in entities]
            await ctx.db.execute(query, ctx.guild.id, entities)

        self.is_plonked.invalidate_containing(f'{ctx.guild.id!r}:')
        await ctx.send(ctx.tick(True))

    @unignore.command(name='all')
    @checks.is_mod()
    async def unignore_all(self, ctx):
        """An alias for ignore clear command."""
        await ctx.invoke(self.ignore_clear)

    @config.group(aliases=['guild'])
    @checks.is_mod()
    async def server(self, ctx):
        """Handles the server-specific permissions."""
        pass

    @config.group()
    @checks.is_mod()
    async def channel(self, ctx):
        """Handles the channel-specific permissions."""
        pass

    async def command_toggle(self, connection, guild_id, channel_id, name, *, whitelist=True):
        # clear the cache
        self.get_command_permissions.invalidate(self, guild_id)

        if channel_id is None:
            subcheck = 'channel_id IS NULL'
            args = (guild_id, name)
        else:
            subcheck = 'channel_id=$3'
            args = (guild_id, name, channel_id)

        async with connection.transaction():
            # delete the previous entry regardless of what it was
            query = f"DELETE FROM command_config WHERE guild_id=$1 AND name=$2 AND {subcheck};"

            # DELETE <num>
            await connection.execute(query, *args)

            query = "INSERT INTO command_config (guild_id, channel_id, name, whitelist) VALUES ($1, $2, $3, $4);"

            try:
                await connection.execute(query, guild_id, channel_id, name, whitelist)
            except asyncpg.UniqueViolationError:
                msg = 'This command is already disabled.' if not whitelist else 'This command is already explicitly enabled.'
                raise RuntimeError(msg)

    @channel.command(name='disable')
    async def channel_disable(self, ctx, *, command: CommandName):
        """Disables a command for this channel."""

        try:
            await self.command_toggle(ctx.db, ctx.guild.id, ctx.channel.id, command, whitelist=False)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send('Command successfully disabled for this channel.')

    @channel.command(name='enable')
    async def channel_enable(self, ctx, *, command: CommandName):
        """Enables a command for this channel."""

        try:
            await self.command_toggle(ctx.db, ctx.guild.id, ctx.channel.id, command, whitelist=True)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send('Command successfully enabled for this channel.')

    @server.command(name='disable')
    async def server_disable(self, ctx, *, command: CommandName):
        """Disables a command for this server."""

        try:
            await self.command_toggle(ctx.db, ctx.guild.id, None, command, whitelist=False)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send('Command successfully disabled for this server')

    @server.command(name='enable')
    async def server_enable(self, ctx, *, command: CommandName):
        """Enables a command for this server."""

        try:
            await self.command_toggle(ctx.db, ctx.guild.id, None, command, whitelist=True)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send('Command successfully enabled for this server.')

    @config.command(name='enable')
    @checks.is_mod()
    async def config_enable(self, ctx, channel: Optional[discord.TextChannel], *, command: CommandName):
        """Enables a command the server or a channel."""

        channel_id = channel.id if channel else None
        human_friendly = channel.mention if channel else 'the server'
        try:
            await self.command_toggle(ctx.db, ctx.guild.id, channel_id, command, whitelist=True)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send(f'Command successfully enabled for {human_friendly}.')

    @config.command(name='disable')
    @checks.is_mod()
    async def config_disable(self, ctx, channel: Optional[discord.TextChannel], *, command: CommandName):
        """Disables a command for the server or a channel."""

        channel_id = channel.id if channel else None
        human_friendly = channel.mention if channel else 'the server'
        try:
            await self.command_toggle(ctx.db, ctx.guild.id, channel_id, command, whitelist=False)
        except RuntimeError as e:
            await ctx.send(e)
        else:
            await ctx.send(f'Command successfully disabled for {human_friendly}.')

    @server.before_invoke
    @channel.before_invoke
    @config_enable.before_invoke
    @config_disable.before_invoke
    async def open_database_before_working(self, ctx):
        await ctx.acquire()

    @config.command(name='disabled')
    @checks.is_mod()
    async def config_disabled(self, ctx, *, channel: discord.TextChannel = None):
        """Shows the disabled commands for the channel given."""

        channel = channel or ctx.channel
        resolved = await self.get_command_permissions(ctx.guild.id)
        disabled = resolved.get_blocked_commands(channel.id)

        if len(disabled) > 15:
            async with self.bot.session.post('https://hastebin.com/documents', data='\n'.join(disabled)) as resp:
                if resp.status != 200:
                    return await ctx.send('Sorry, failed to post data to hastebin.')
                js = await resp.json()
                value = f'Too long... Check: https://hastebin.com/{js["key"]}.txt'
        else:
            value = '\n'.join(disabled) or 'None!'
        await ctx.send(f'In {channel.mention} the following commands are disabled:\n{value}')


    @config.group(name='global')
    @commands.is_owner()
    async def _global(self, ctx):
        """Handles global bot configuration."""
        pass

    @_global.command(name='block')
    async def global_block(self, ctx, object_id: int):
        """Blocks a user or guild globally."""
        await self.bot.add_to_blacklist(object_id)
        await ctx.send(ctx.tick(True))

    @_global.command(name='unblock')
    async def global_unblock(self, ctx, object_id: int):
        """Unblocks a user or guild globally."""
        await self.bot.remove_from_blacklist(object_id)
        await ctx.send(ctx.tick(True))

def setup(bot):
    bot.add_cog(Config(bot))