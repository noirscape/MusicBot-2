import asyncio
import functools
import logging
import os
import pathlib
import re
import json

import discord
import discord.ext.commands as commands
from yarl import URL
import youtube_dl


def setup(bot):
    bot.add_cog(Music(bot))


def duration_to_str(duration):
    # Extract minutes, hours and days
    minutes, seconds = divmod(duration, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    # Create a fancy string
    duration = []
    if days > 0: duration.append('{} days'.format(days))
    if hours > 0: duration.append('{} hours'.format(hours))
    if minutes > 0: duration.append('{} minutes'.format(minutes))
    if seconds > 0 or len(duration) == 0: duration.append('{} seconds'.format(seconds))

    return ', '.join(duration)


class MusicError(commands.UserInputError):
    pass


class Song(discord.PCMVolumeTransformer):
    def __init__(self, song_info):
        self.info = song_info.info
        self.requester = song_info.requester
        self.channel = song_info.channel
        self.filename = song_info.filename
        self.playing_string = str(song_info) #hacky fix
        super().__init__(discord.FFmpegPCMAudio(self.filename, before_options='-nostdin', options='-vn'))

    def __str__(self):
        return self.playing_string #hacky fix

class SongInfo:
    ytdl_opts = {
        'default_search': 'auto',
        'format': 'bestaudio/best',
        'ignoreerrors': True,
        'source_address': '0.0.0.0', # Make all connections via IPv4
        'nocheckcertificate': True,
        'restrictfilenames': True,
        'logger': logging.getLogger(__name__),
        'logtostderr': False,
        'no_warnings': True,
        'quiet': True,
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'noplaylist': True
    }
    ytdl = youtube_dl.YoutubeDL(ytdl_opts)

    def __init__(self, info, requester, channel):
        self.info = info
        self.requester = requester
        self.channel = channel
        self.filename = info.get('_filename', self.ytdl.prepare_filename(self.info))
        self.downloaded = asyncio.Event()
        self.local_file = '_filename' in info

    @classmethod
    async def create(cls, query, requester, channel, loop=None):
        try:
            # Path.is_file() can throw a OSError on syntactically incorrect paths, like urls.
            if pathlib.Path(query).is_file():
                return cls.from_file(query, requester, channel)
        except OSError:
            pass

        return await cls.from_ytdl(query, requester, channel, loop=loop)

    @classmethod
    def from_file(cls, file, requester, channel):
        path = pathlib.Path(file)
        if not path.exists():
            raise MusicError('File {} not found.'.format(file))

        info = {
            '_filename': file,
            'title': path.stem,
            'creator': 'local file',
        }
        return cls(info, requester, channel)

    @classmethod
    async def from_ytdl(cls, request, requester, channel, loop=None):
        loop = loop or asyncio.get_event_loop()

        # Get sparse info about our query
        partial = functools.partial(cls.ytdl.extract_info, request, download=False, process=False)
        sparse_info = await loop.run_in_executor(None, partial)

        if sparse_info is None:
            raise MusicError('Could not retrieve info from input : {}'.format(request))

        # If we get a playlist, select its first valid entry
        if "entries" not in sparse_info:
            info_to_process = sparse_info
        else:
            info_to_process = None
            for entry in sparse_info['entries']:
                if entry is not None:
                    info_to_process = entry
                    break
            if info_to_process is None:
                raise MusicError('Could not retrieve info from input : {}'.format(request))

        # Process full video info
        url = info_to_process.get('url', info_to_process.get('webpage_url', info_to_process.get('id')))
        partial = functools.partial(cls.ytdl.extract_info, url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise MusicError('Could not retrieve info from input : {}'.format(request))

        # Select the first search result if any
        if "entries" not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise MusicError('Could not retrieve info from url : {}'.format(info_to_process["url"]))

        return cls(info, requester, channel)

    async def download(self, loop):
        if not pathlib.Path(self.filename).exists():
            partial = functools.partial(self.ytdl.extract_info, self.info['webpage_url'], download=True)
            self.info = await loop.run_in_executor(None, partial)
        self.downloaded.set()

    async def wait_until_downloaded(self):
        await self.downloaded.wait()

    def __str__(self):
        title = "`{}`".format(self.info['title'])
        creator = "`{}`".format(self.info.get('creator') or self.info['uploader'])
        duration = " (duration: {})".format(duration_to_str(self.info['duration'])) if 'duration' in self.info else ''
        requester = "`{}`".format(self.requester)
        return '{} from {}{} added by {}'.format(title, creator, duration, requester)


class Playlist(asyncio.Queue):
    def __iter__(self):
        return self._queue.__iter__()

    def clear(self):
        for song in self._queue:
            try:
                os.remove(song.filename)
            except:
                pass
        self._queue.clear()

    def get_song(self):
        return self.get_nowait()

    def add_song(self, song):
        self.put_nowait(song)

    def delete_song(self, idx):
        del self._queue[idx]

    def __str__(self):
        info = 'Current playlist:\n'
        info_len = len(info)
        for idx, song in enumerate(self, 1):
            s = '{}. {}'.format(idx, str(song))
            l = len(s) + 1 # Counting the extra \n
            if info_len + l > 1995:
                info += '[...]'
                break
            info += '{}\n'.format(s)
            info_len += l
        return info


class GuildMusicState:
    def __init__(self, bot):
        self.bot = bot
        self.playlist = Playlist(maxsize=50)
        self.voice_client = None
        self.loop = bot.loop
        self.player_volume = 0.5
        self.skips = set()
        self.min_skips = 5

    @property
    def current_song(self):
        return self.voice_client.source

    @property
    def volume(self):
        return self.player_volume

    @volume.setter
    def volume(self, value):
        self.player_volume = value
        if self.voice_client:
            self.voice_client.source.volume = value

    async def stop(self):
        self.playlist.clear()
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None

    def is_playing(self):
        return self.voice_client and self.voice_client.is_playing()

    async def play_next_song(self, song=None, error=None):
        if error:
            await self.current_song.channel.send('An error has occurred while playing {}: {}'.format(self.current_song, error))

        if song and not song.local_file and song.filename not in [s.filename for s in self.playlist]:
            os.remove(song.filename)

        self.skips.clear()

        if self.playlist.empty():
            await self.stop()
            await self.bot.change_presence(activity=None)
        else:
            next_song_info = self.playlist.get_song()
            await next_song_info.wait_until_downloaded()
            source = Song(next_song_info)
            source.volume = self.player_volume
            self.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next_song(next_song_info, e), self.loop).result())
            await next_song_info.channel.send('Now playing {}'.format(next_song_info))
            await self.bot.change_presence(activity=discord.Game(name=next_song_info.info["title"]))


class Music:
    def __init__(self, bot):
        self.bot = bot
        self.music_states = {}
        with open('blacklist.json') as blacklist:
            blacklist_dict = json.load(blacklist)
            self.blacklisted_users = set(blacklist_dict["users"])
            self.blacklisted_videos = set(blacklist_dict["videos"])

    def __unload(self):
        for state in self.music_states.values():
            self.bot.loop.create_task(state.stop())

    def __local_check(self, ctx):
        if not ctx.guild:
            raise commands.NoPrivateMessage('This command cannot be used in a private message.')
        return True

    async def __before_invoke(self, ctx):
        ctx.music_state = self.get_music_state(ctx.guild.id)

    async def __error(self, ctx, error):
        if not isinstance(error, commands.UserInputError):
            raise error

        try:
            await ctx.send(error)
        except discord.Forbidden:
            pass # /shrug

    def get_music_state(self, guild_id):
        return self.music_states.setdefault(guild_id, GuildMusicState(self.bot))

    async def can_content_be_played(self, song: SongInfo):
        if song.info["duration"] > self.bot.config["song_length"]:
            return None, None, True
        for blacklisted_item in self.blacklisted_videos:
            if blacklisted_item in song.info["title"] or blacklisted_item in song.info["description"] or blacklisted_item in song.info["id"] or blacklisted_item in song.info["uploader"]:
                return blacklisted_item, False, False
        return None, True, False

    def has_super_powers():
        async def predicate(ctx):
            user_role_list = [x.name for x in ctx.author.roles]
            return "Helpers" in user_role_list or "Staff" in user_role_list
        return commands.check(predicate)

    @commands.command(aliases=['np'])
    async def status(self, ctx):
        """Displays the currently played song."""
        if ctx.music_state.is_playing():
            song = ctx.music_state.current_song
            await ctx.send('Playing {}. Volume at {} in {}'.format(song, str(song.volume * 100), ctx.voice_client.channel.mention))
        else:
            await ctx.send('Not playing.')

    @commands.command(aliases=['queue'])
    async def playlist(self, ctx):
        """Shows info about the current playlist and currently playing track."""
        await ctx.invoke(self.bot.get_command('status'))
        await ctx.send('{}'.format(ctx.music_state.playlist))

    @commands.command()
    @has_super_powers()
    async def join(self, ctx, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.

        If no channel is given, summons it to your current voice channel.
        
        Staff & Helpers only."""
        if channel is None and not ctx.author.voice:
            raise MusicError('You are not in a voice channel nor specified a voice channel for me to join.')

        destination = channel or ctx.author.voice.channel

        if ctx.voice_client:
            await ctx.voice_client.move_to(destination)
        else:
            ctx.music_state.voice_client = await destination.connect()

    @commands.command()
    async def play(self, ctx, *, request: str):
        """Plays a song or adds it to the playlist.

        Automatically searches with youtube_dl
        List of supported sites :
        https://github.com/rg3/youtube-dl/blob/1b6712ab2378b2e8eb59f372fb51193f8d3bdc97/docs/supportedsites.md
        """
        if ctx.author.id in self.blacklisted_users:
            raise MusicError('Cannot add track, {} has been blacklisted.'.format(ctx.author))
        await ctx.message.add_reaction('\N{HOURGLASS}')

        # Create the SongInfo
        song = await SongInfo.create(request, ctx.author, ctx.channel, loop=ctx.bot.loop)

        # Check if song can be played
        _, blacklist_status, video_too_long = await self.can_content_be_played(song)
        if video_too_long:
            raise MusicError('Video is too long (`{}` > `{}`)'.format(song.info["duration"], self.bot.config["song_length"]))
        if not blacklist_status:
            raise MusicError('Video content has been blacklisted. If you believe this to be in error, contact staff.')

        # Connect to the voice channel if needed
        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            try:
                ctx.music_state.voice_client = await ctx.guild.get_channel(self.bot.config['voice_channel'][ctx.guild.id]).connect()
            except KeyError:
                await ctx.invoke(self.join)

        # Add the info to the playlist
        try:
            ctx.music_state.playlist.add_song(song)
        except asyncio.QueueFull:
            raise MusicError('Playlist is full, try again later.')

        if not ctx.music_state.is_playing():
            # Download the song and play it
            await song.download(ctx.bot.loop)
            await ctx.music_state.play_next_song()
        else:
            # Schedule the song's download
            ctx.bot.loop.create_task(song.download(ctx.bot.loop))
            await ctx.send('Queued {} in position **#{}**'.format(song, ctx.music_state.playlist.qsize()))

        await ctx.message.remove_reaction('\N{HOURGLASS}', ctx.me)
        await ctx.message.add_reaction('\N{WHITE HEAVY CHECK MARK}')

    @play.error
    async def play_error(self, ctx, error):
        await ctx.message.remove_reaction('\N{HOURGLASS}', ctx.me)
        await ctx.message.add_reaction('\N{CROSS MARK}')
        self.bot.logger.exception("Something went wrong:")

    @commands.command(name='remove') # Weird?
    @has_super_powers()
    async def remove_song(self, ctx, idx: int):
        """Removes the song at the position in the autoplaylist.

        Staff & Helpers only."""
        if idx < 0:
            raise MusicError('Position must be above 0!')
        try:
            ctx.music_state.playlist.delete_song(idx - 1)
        except IndexError:
            raise MusicError('Invalid song position.')
        else:
            await ctx.send('Song removed from playlist!')

    @commands.group(invoke_without_command=True)
    @has_super_powers()
    async def blacklist(self, ctx):
        """Blacklist commands

        Staff & Helpers only."""
        await ctx.invoke(self.bot.get_command('help'), *ctx.command.qualified_name.split())

    @blacklist.group(invoke_without_command=True)
    @has_super_powers()
    async def user(self, ctx):
        """Blacklist users/remove them from the blacklist.

        Staff & Helpers only."""
        await ctx.invoke(self.bot.get_command('help'), *ctx.command.qualified_name.split())

    @user.command(name='add', aliases=['+'])
    @has_super_powers()
    async def user_add(self, ctx, user: discord.User):
        """Adds a user to the blacklist

        Staff & Helpers only."""
        if user.id not in self.blacklisted_users:
            self.blacklisted_users.add(user.id)
            with open('blacklist.json', 'w') as blacklist_file:
                json.dump({"users": list(self.blacklisted_users), "videos": list(self.blacklisted_videos)}, blacklist_file)
            return await ctx.send('Successfully blacklisted user `{}`!'.format(str(user)))
        else:
            return await ctx.send('User already blacklisted.')

    @user.command(name='remove', aliases=['-'])
    @has_super_powers()
    async def user_remove(self, ctx, user: discord.User):
        """Removes a user from the blacklist.

        Staff & Helpers only."""
        try:
            self.blacklisted_users.remove(user.id)
        except KeyError:
            return await ctx.send('User not blacklisted.')
        else:
            with open('blacklist.json', 'w') as blacklist_file:
                json.dump({"users": list(self.blacklisted_users), "videos": list(self.blacklisted_videos)}, blacklist_file)
            return await ctx.send('Successfully removed user {} from blacklist!'.format(str(user)))

    @user.command(name='show')
    @has_super_powers()
    async def user_show(self, ctx):
        """DMs the blacklist.

        Staff & Helpers only."""
        paginator = commands.Paginator(prefix='', suffix='')
        paginator.add_line('___Blacklisted users___')
        for blacklisted_user in self.blacklisted_users:
            paginator.add_line('{} (ID: {})'.format(str(await self.bot.get_user_info(blacklisted_user)), blacklisted_user))
        for page in paginator.pages:
            await ctx.author.send(page)

    @blacklist.group(invoke_without_command=True)
    @has_super_powers()
    async def video(self, ctx):
        """Blacklist videos.

        Staff & Helpers only."""
        await ctx.invoke(self.bot.get_command('help'), *ctx.command.qualified_name.split())

    @video.command(name='add', aliases=['+'])
    @has_super_powers()
    async def video_add(self, ctx, string):
        """Adds a video to the blacklist.

        The blacklist can contains video IDs or words that will be matched in the title or description. 
        If any of the words is in the blacklist, the video will not play.
        
        Staff & Helpers only."""

        # Check if argument given is a valid YouTube URL first.
        if re.search(r"https?://(?:www\.)?(youtube|youtu\.be)", string, re.I):
            url = URL(string)
            if url.host == "youtu.be":
                string = url.path[1:]
            else:
                string = url.query['v']

        if string not in self.blacklisted_videos:
            self.blacklisted_videos.add(string)
            with open('blacklist.json', 'w') as blacklist_file:
                json.dump({"users": list(self.blacklisted_users), "videos": list(self.blacklisted_videos)}, blacklist_file)
            return await ctx.send('Successfully blacklisted video content `{}`!'.format(string))
        else:
            return await ctx.send('Video content already on blacklist.')


    @video.command(name='remove', aliases=['-'])
    @has_super_powers()
    async def video_remove(self, ctx, string):
        """Removes a video from the blacklist.

        Staff & Helpers only."""

        # Check if argument given is a valid YouTube URL first.
        if re.search(r"https?://(?:www\.)?(youtube|youtu\.be)", string, re.I):
            url = URL(string)
            if url.host == "youtu.be":
                string = url.path[1:]
            else:
                string = url.query['v']
        try:
            self.blacklisted_videos.remove(string)
        except KeyError:
            return await ctx.send('Video content not blacklisted.')
        else:
            with open('blacklist.json', 'w') as blacklist_file:
                json.dump({"users": list(self.blacklisted_users), "videos": list(self.blacklisted_videos)}, blacklist_file)
            return await ctx.send('Successfully removed video content `{}` from blacklist!'.format(string))

    @video.command(name='show')
    @has_super_powers()
    async def video_show(self, ctx):
        """DMs the blacklist.

        Staff & Helpers only."""
        paginator = commands.Paginator(prefix='', suffix='')
        paginator.add_line('___Blacklisted video content___')
        for blacklisted_video in self.blacklisted_videos:
            paginator.add_line('- `{}`'.format(blacklisted_video))
        for page in paginator.pages:
            await ctx.author.send(page)

    @commands.command()
    @has_super_powers()
    async def pause(self, ctx):
        """Pauses the player.
        
        Staff & Helpers only."""
        if ctx.voice_client:
            ctx.voice_client.pause()

    @commands.command()
    @has_super_powers()
    async def resume(self, ctx):
        """Resumes the player.
        
        Staff & Helpers only."""
        if ctx.voice_client:
            ctx.voice_client.resume()

    @commands.command()
    @has_super_powers()
    async def stop(self, ctx):
        """Stops the player, clears the playlist and leaves the voice channel.
        
        Staff & Helpers only."""
        await ctx.music_state.stop()

    @commands.command()
    @has_super_powers()
    async def volume(self, ctx, volume: int = None):
        """Sets the volume of the player, scales from 0 to 100.
        
        Staff & Helpers only."""
        if volume < 0 or volume > 100:
            raise MusicError('The volume level has to be between 0 and 100.')
        ctx.music_state.volume = volume / 100

    @commands.command()
    @has_super_powers()
    async def clear(self, ctx):
        """Clears the playlist.
        
        Staff & Helpers only."""
        ctx.music_state.playlist.clear()

    @commands.command()
    async def skip(self, ctx):
        """Votes to skip the current song.

        If you are the one who added the song, you will skip instantly.

        To configure the minimum number of votes needed, use `minskips`
        """
        if ctx.author.id in self.blacklisted_users:
            raise MusicError('Cannot skip track, {} has been blacklisted.'.format(ctx.author))

        if not ctx.music_state.is_playing():
            raise MusicError('Not playing anything to skip.')

        if ctx.author.id in ctx.music_state.skips:
            raise MusicError('{} You already voted to skip that song'.format(ctx.author.mention))

        if ctx.author not in ctx.music_state.voice_client.channel.members:
            raise MusicError('You are not in the voice channel.')

        # Count the vote
        ctx.music_state.skips.add(ctx.author.id)
        await ctx.message.add_reaction('\N{WHITE HEAVY CHECK MARK}')

        # Get total amount of members in voice channel, exempting the bot.
        listeners = len(ctx.music_state.voice_client.channel.members) - 1

        # Calculate if percentage to skip matches
        percentage_skip = len(ctx.music_state.skips) >= listeners * self.bot.config["percentage_skip"]

        # Check if the song has to be skipped
        if len(ctx.music_state.skips) > ctx.music_state.min_skips or percentage_skip or ctx.author == ctx.music_state.current_song.requester:
            ctx.music_state.skips.clear()
            ctx.voice_client.stop()

    @commands.command()
    @has_super_powers()
    async def force_skip(self, ctx):
        """Forcibly skips the current song.

        Staff & Helpers only."""
        ctx.music_state.skips.clear()
        ctx.voice_client.stop()

    @commands.command()
    @has_super_powers()
    async def minskips(self, ctx, number: int):
        """Sets the minimum number of votes to skip a song.

        Staff & Helpers only."""
        ctx.music_state.min_skips = number
