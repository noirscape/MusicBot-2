import discord
from discord.ext import commands
from git import Repo
import os
import subprocess
import sys
import aiohttp
from utils.superpowers import is_special_owner

def setup(bot):
    bot.add_cog(Git(bot))

class Git(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.repo = Repo(os.getcwd())

    async def hastebin(self, content):
        """Upload output to hastebin

        Taken from appu's selfbot.

        Arguments:
            content (str): String to upload.
        """
        async with aiohttp.ClientSession() as session:
            async with session.post("https://hastebin.com/documents", data=content) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return "https://hastebin.com/" + result["key"]
                else:
                    return "Error with creating Hastebin. Status: %s" % resp.status

    @commands.group()
    @is_special_owner()
    async def git(self, ctx):
        """Update the bot."""
        if ctx.invoked_subcommand is None:
            await ctx.invoke(self.bot.get_command('help'), ctx.command.qualified_name)

    @git.command()
    @is_special_owner()
    async def pull(self, ctx):
        """Pull the GitHub repo
        """
        output = self.repo.git.pull()
        await ctx.author.send('Pulled changes:\n```' + output + '```')

    @git.command()
    @is_special_owner()
    async def update_requirements(self, ctx):
        """Use pip to update the requirements.
        """
        process = subprocess.Popen([sys.executable + " -m pip install -U -r requirements.txt"], shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()

        h_stdout = await self.hastebin(stdout.decode('utf-8'))
        h_stderr = await self.hastebin(stderr.decode('utf-8'))

        await ctx.author.send(f'Updated requirements:\nstdout: {h_stdout}\nstderr: {h_stderr}')

    @git.command()
    @is_special_owner()
    async def update(self, ctx):
        """General update command.

        Pulls changes, updates requirements and stops the bot."""
        await ctx.author.send('Pulling changes...')
        await ctx.invoke(self.bot.get_command('git pull'))
        await ctx.author.send('Updating requirements...')
        await ctx.invoke(self.bot.get_command('git update_requirements'))
        await ctx.author.send('Stopping bot...')
        await ctx.invoke(self.bot.get_command('exit'))

    @commands.command()
    @is_special_owner()
    async def exit(self, ctx):
        """Log the bot out, ending the blocking call and stopping the bot.
        """
        await self.bot.logout()
