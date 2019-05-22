from discord.ext import commands

def has_super_powers():
    return commands.check(not_check_has_super_powers)

async def not_check_has_super_powers(ctx: commands.Context):
    user_role_list = [x.name for x in ctx.author.roles]
    return "Helpers" in user_role_list or "Staff" in user_role_list

def is_special_owner():
    return commands.check(not_check_is_special_owner)

async def not_check_is_special_owner(ctx: commands.Context):
    app_info = await ctx.bot.application_info()
    if ctx.author.id == app_info.owner.id: # If we're owner we're owner.
        return True
    owner_role_name = ctx.bot.config["owner_role"]
    user_role_list = [x.name for x in ctx.author.roles]
    return owner_role_name in user_role_list