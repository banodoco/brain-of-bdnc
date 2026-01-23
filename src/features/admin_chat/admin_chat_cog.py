"""Discord cog for admin chat - handles DMs from ADMIN_USER_ID.

Listens for DMs from the admin and processes them through the Claude agent.
"""
import os
import logging
import discord
from discord.ext import commands

from .agent import AdminChatAgent

logger = logging.getLogger('DiscordBot')


class AdminChatCog(commands.Cog):
    """Cog that handles admin DM conversations with Claude."""
    
    def __init__(self, bot: commands.Bot, db_handler, sharer):
        self.bot = bot
        self.db_handler = db_handler
        self.sharer = sharer
        self.agent: AdminChatAgent = None
        
        # Get admin user ID
        admin_id_str = os.getenv('ADMIN_USER_ID')
        if admin_id_str:
            try:
                self.admin_user_id = int(admin_id_str)
                logger.info(f"[AdminChat] Configured for admin user ID: {self.admin_user_id}")
            except ValueError:
                logger.error(f"[AdminChat] Invalid ADMIN_USER_ID: {admin_id_str}")
                self.admin_user_id = None
        else:
            logger.warning("[AdminChat] ADMIN_USER_ID not set - admin chat disabled")
            self.admin_user_id = None
    
    def _ensure_agent(self):
        """Lazily initialize the agent (to avoid issues during bot startup)."""
        if self.agent is None:
            try:
                self.agent = AdminChatAgent(
                    bot=self.bot,
                    db_handler=self.db_handler,
                    sharer=self.sharer
                )
                logger.info("[AdminChat] Agent initialized")
            except Exception as e:
                logger.error(f"[AdminChat] Failed to initialize agent: {e}", exc_info=True)
                raise
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for DMs from the admin user."""
        
        # Ignore messages from bots (including self)
        if message.author.bot:
            return
        
        # Only respond to DMs
        if not isinstance(message.channel, discord.DMChannel):
            return
        
        # Only respond to the admin user
        if self.admin_user_id is None or message.author.id != self.admin_user_id:
            return
        
        # Ignore empty messages
        if not message.content.strip():
            return
        
        logger.info(f"[AdminChat] Received DM from admin: {message.content[:50]}...")
        
        try:
            # Initialize agent if needed
            self._ensure_agent()
            
            # Show typing indicator while processing
            async with message.channel.typing():
                responses = await self.agent.chat(
                    user_id=message.author.id,
                    user_message=message.content
                )
            
            # responses is a list of messages, or None if ended without reply
            if responses is None:
                logger.info("[AdminChat] Turn ended without reply (silent action)")
                return
            
            # Send each response message
            total_chars = 0
            for response in responses:
                total_chars += len(response)
                # Handle long messages by splitting
                if len(response) <= 2000:
                    await message.channel.send(response)
                else:
                    # Split into chunks
                    chunks = [response[i:i+1990] for i in range(0, len(response), 1990)]
                    for chunk in chunks:
                        await message.channel.send(chunk)
            
            logger.info(f"[AdminChat] Sent {len(responses)} message(s) ({total_chars} chars total)")
            
        except Exception as e:
            logger.error(f"[AdminChat] Error processing message: {e}", exc_info=True)
            await message.channel.send(f"Sorry, I encountered an error: {str(e)}")
    
    @commands.command(name='adminchat_clear')
    @commands.is_owner()
    async def clear_history(self, ctx: commands.Context):
        """Clear the admin chat conversation history."""
        if self.agent:
            self.agent.clear_conversation(ctx.author.id)
            await ctx.send("Conversation history cleared.")
        else:
            await ctx.send("Agent not initialized.")


async def setup(bot: commands.Bot):
    """Setup function for loading the cog."""
    # These will be passed from main.py
    db_handler = getattr(bot, 'db_handler', None)
    sharer = getattr(bot, 'sharer', None)
    
    if db_handler is None or sharer is None:
        logger.error("[AdminChat] Cannot setup cog - db_handler or sharer not found on bot")
        return
    
    await bot.add_cog(AdminChatCog(bot, db_handler, sharer))
    logger.info("[AdminChat] Cog loaded")
