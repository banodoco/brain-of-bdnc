import discord
from discord.ext import commands
import os
from typing import List, Dict, Tuple
import asyncio
from dotenv import load_dotenv
import re
from datetime import datetime, timedelta
import json
import logging
import sys
import aiohttp
from src.common.db_handler import DatabaseHandler
from src.common.base_bot import BaseDiscordBot
from src.common.llm import get_llm_response

class SearchAnswerBot(BaseDiscordBot):
    def __init__(self):
        # Initialize logger first
        self.logger = logging.getLogger('search_bot')
        self.logger.setLevel(logging.INFO)
        
        # Set up intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        
        # Initialize the bot with required parameters
        super().__init__(
            command_prefix="!",
            intents=intents,
            heartbeat_timeout=120.0,
            guild_ready_timeout=30.0,
            gateway_queue_size=512,
            logger=self.logger
        )
        
        # Set up logging before any other operations
        self.setup_logging()
        
        # Ensure environment variables are loaded
        load_dotenv()
        
        # Add error handling for required environment variables
        required_env_vars = ['ANTHROPIC_API_KEY', 'GUILD_ID', 'DISCORD_BOT_TOKEN', 'ADMIN_USER_ID']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
            
        self.answer_channel_id = 1322583491019407361
        self.guild_id = int(os.getenv('GUILD_ID'))
        self.channel_map = {}
        
        # Add rate limiting
        self.search_cooldown = commands.CooldownMapping.from_cooldown(
            2, # Number of searches
            60.0, # Per 60 seconds
            commands.BucketType.user
        )

    def setup_logging(self):
        """Setup logging configuration with file rotation."""
        # Define log file path with absolute path
        log_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(log_dir, 'search_dev_logs.log')
        
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            
            # Test file creation/writing permissions
            with open(log_file, 'w') as f:
                f.write("Initializing log file\n")
            
            print(f"Created log file at: {log_file}")
            
            # Configure logging
            logging.basicConfig(
                level=logging.DEBUG,
                format='%(asctime)s - %(levelname)s - %(message)s',
                handlers=[
                    logging.FileHandler(log_file, mode='a'),  # Changed to append mode
                    logging.StreamHandler(sys.stdout)
                ]
            )
            
            # Test logging
            logging.info("Logging system initialized")
            
        except Exception as e:
            print(f"Error setting up logging: {e}")
            print(f"Attempted to create log at: {log_file}")
            # Fallback to just console logging
            logging.basicConfig(
                level=logging.DEBUG,
                format='%(asctime)s - %(levelname)s - %(message)s',
                handlers=[logging.StreamHandler(sys.stdout)]
            )

    async def setup_hook(self):
        logging.info(f"Bot is setting up...")
        
    async def on_ready(self):
        logging.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logging.info(f'Connected to {len(self.guilds)} guilds')
        logging.info(f'Watching for questions in channel ID: {self.answer_channel_id}')
        logging.info('------')

    async def get_searchable_channels(self) -> Dict[int, str]:
        """Get all text channels in the guild that are not support channels."""
        guild = self.get_guild(self.guild_id)
        if not guild:
            logging.warning(f"Warning: Could not find guild with ID {self.guild_id}")
            return {}
            
        channels = {}
        for channel in guild.text_channels:
            # Skip channels with 'support' in the name
            if 'support' not in channel.name.lower():
                channels[channel.id] = channel.name
                
        # Add debug logging
        logging.info(f"Found {len(channels)} searchable channels:")
        for channel_id, channel_name in channels.items():
            logging.debug(f"- #{channel_name} (ID: {channel_id})")
            
        return channels

    async def determine_relevant_channels(self, question: str, channels: Dict[int, str], search_info: Dict) -> List[int]:
        """Ask the LLM dispatcher which channels are most relevant for the search."""
        # Define system prompt
        system_prompt = """Given this question and list of Discord channels, return ONLY the channel IDs that are most relevant for finding the answer.
Format as a JSON list of integers. Example: [123456789, 987654321]. Return ONLY the list of relevant channel IDs, nothing else."""
        
        # Prepare user content
        user_content = f"Question: {question}\n\nAvailable channels:\n{json.dumps(channels, indent=2)}"
        messages = [{"role": "user", "content": user_content}]
        
        search_info['step_channel_selection_start'] = datetime.now()
        
        try:
            self.logger.info(f"Asking LLM (Claude Haiku) to determine relevant channels for: '{question[:50]}...'")
            # Call the dispatcher directly (no need for run_in_executor)
            response_text = await get_llm_response(
                client_name="claude",
                model="claude-sonnet-4-5-20250929",
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500 # Keep max_tokens reasonable
            )
            
            self.logger.debug(f"LLM response for channel selection: {response_text}")
            search_info['step_channel_selection_llm_complete'] = datetime.now()
            
            # Try to parse as JSON
            try:
                channel_ids = json.loads(response_text.strip())
                if isinstance(channel_ids, list):
                    # Filter to only valid channel IDs
                    valid_ids = [cid for cid in channel_ids if cid in channels]
                    if valid_ids:
                        self.logger.info(f"LLM identified relevant channels: {valid_ids}")
                        search_info['selected_channels'] = valid_ids
                        search_info['step_channel_selection_complete'] = datetime.now()
                        return valid_ids
                    else:
                         self.logger.warning("LLM returned empty list or only invalid channel IDs.")
            except json.JSONDecodeError:
                self.logger.error(f"Failed to parse LLM response for channel selection as JSON: {response_text}")
            
            # If parsing failed or no valid IDs returned
            self.logger.warning("Invalid response format or no valid channels from LLM. Falling back to first 3 channels.")
            fallback_channels = list(channels.keys())[:3]
            search_info['selected_channels'] = fallback_channels
            search_info['step_channel_selection_complete'] = datetime.now()
            return fallback_channels
            
        except Exception as e:
            # Catch errors from the dispatcher
            self.logger.error(f"Error determining relevant channels via LLM dispatcher: {e}", exc_info=True)
            search_info['step_channel_selection_error'] = str(e)
            search_info['step_channel_selection_complete'] = datetime.now()
            # Fallback: return empty list or first few? Empty seems safer.
            return []

    async def generate_search_queries(self, question: str, search_info: Dict) -> List[Dict[str, str]]:
        """Generate search queries based on the question using the LLM dispatcher."""
        
        system_prompt = """Generate 2-3 precise search queries for finding information in Discord channels.
Rules:
- Keep queries very short (1-3 words)
- Focus on the most specific, relevant terms
- Avoid generic terms unless necessary
- Include technical terms if relevant
- Prioritize exact matches over broad concepts

Format as JSON list with 'query' and 'reason' keys.
Example for "How do I adjust video settings in Hunyuan?":
[
    {"query": "video settings", "reason": "Most specific match for the question"},
    {"query": "resolution config", "reason": "Alternative technical term"}
]

Return ONLY the JSON list."""
        
        user_content = f"Question: {question}"
        messages = [{"role": "user", "content": user_content}]
        
        search_info['step_query_generation_start'] = datetime.now()
        
        try:
            self.logger.info(f"Asking LLM (Claude Haiku) to generate search queries for: '{question[:50]}...'")
            # Call the dispatcher directly
            response_text = await get_llm_response(
                client_name="claude",
                model="claude-sonnet-4-5-20250929",
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500
            )
            
            self.logger.debug(f"LLM response for query generation: {response_text}")
            search_info['step_query_generation_llm_complete'] = datetime.now()
            
            # Try to parse as JSON
            try:
                queries = json.loads(response_text.strip())
                if isinstance(queries, list) and all(isinstance(q, dict) and 'query' in q and 'reason' in q for q in queries):
                    self.logger.info(f"LLM generated queries: {queries}")
                    search_info['generated_queries'] = queries
                    search_info['step_query_generation_complete'] = datetime.now()
                    return queries
                else:
                     self.logger.warning(f"LLM generated query list has invalid format: {queries}")
            except json.JSONDecodeError:
                self.logger.error(f"Failed to parse LLM response for query generation as JSON: {response_text}")
            
            # Fallback if parsing fails or format is wrong
            self.logger.warning("Invalid response format from LLM for queries. Falling back to original question.")
            fallback_query = [{"query": question, "reason": "Fallback to original question due to LLM format error"}]
            search_info['generated_queries'] = fallback_query
            search_info['step_query_generation_complete'] = datetime.now()
            return fallback_query
            
        except Exception as e:
            # Catch errors from the dispatcher
            self.logger.error(f"Error generating search queries via LLM dispatcher: {e}", exc_info=True)
            search_info['step_query_generation_error'] = str(e)
            search_info['step_query_generation_complete'] = datetime.now()
            fallback_query = [{"query": question, "reason": "Error occurred during LLM call, using original question"}]
            return fallback_query

    async def search_channels(self, query: str, channels: List[int], limit: int = None) -> List[discord.Message]:
        """Search for messages in specified channels using archived data first."""
        self.logger.info(f"Searching channels for query: {query}")
        
        results = []
        db = None

        try:
            db = DatabaseHandler()
            for channel_id in channels:
                archived_messages = db.search_messages(query, channel_id)
                
                # Convert archived messages back to discord.Message objects
                for msg_data in archived_messages:
                    channel = self.get_channel(msg_data['channel_id'])
                    if channel:
                        # Create partial Message object from archived data
                        message = discord.PartialMessage(
                            channel=channel,
                            id=msg_data['id']
                        )
                        # Fetch full message if needed
                        try:
                            full_message = await message.fetch()
                            results.append(full_message)
                        except discord.NotFound:
                            # Message was deleted, use archived data
                            message._update(msg_data)
                            results.append(message)
                            
                self.logger.info(f"Found {len(archived_messages)} archived matches in #{channel.name}")
                
            # If we didn't find enough results in archive, search recent messages
            if len(results) < (limit or 100):
                recent_results = await self._search_recent_messages(query, channels, limit)
                results.extend(recent_results)
                
        except Exception as e:
            self.logger.error(f"Error searching archive: {e}")
            # Fallback to searching recent messages
            results = await self._search_recent_messages(query, channels, limit)
        
        finally:
            if db:
                db.close()
        
        return results

    async def _search_recent_messages(self, query: str, channels: List[int], limit: int = None) -> List[discord.Message]:
        """Search only recent messages using Discord API."""
        results = []
        for channel_id in channels:
            try:
                channel = self.get_channel(channel_id)
                if not channel:
                    self.logger.warning(f"Could not find channel {channel_id}")
                    continue
                    
                self.logger.info(f"Searching recent messages in #{channel.name}")
                
                # Add delay between channel searches to avoid rate limits
                await asyncio.sleep(1)
                
                message_count = 0
                async for message in channel.history(limit=limit or 100):
                    message_count += 1
                    if query.lower() in message.content.lower():
                        results.append(message)
                        
                    if message_count % 100 == 0:
                        await asyncio.sleep(1)  # Rate limiting delay
                        
                self.logger.info(f"Found {len(results)} recent matches in #{channel.name}")
                
            except Exception as e:
                self.logger.error(f"Error searching channel {channel_id}: {e}")
                
        return results

    def format_messages_for_context(self, messages: List[discord.Message]) -> str:
        """Format messages for Claude context."""
        context = []
        for msg in messages:
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            channel_name = msg.channel.name if msg.channel else "unknown-channel"
            context.append(f"[{timestamp}] #{channel_name} - {msg.author.name}: {msg.content}")
            if msg.attachments:
                context.append(f"[Attachments: {', '.join(a.filename for a in msg.attachments)}]")
            jump_url = self.generate_jump_url(msg.guild.id, msg.channel.id, msg.id)
            context.append(f"Message Link: {jump_url}\n")
        return "\n".join(context)

    async def get_claude_answer(self, question: str, context: str, search_info: Dict) -> str:
        """Generate an answer using Claude based on the question and context using the LLM dispatcher."""
        
        system_prompt = """You are an AI assistant answering questions based ONLY on the provided Discord message context. Follow these rules:
1. Answer concisely and directly based *only* on the text provided in the context.
2. If the context doesn't contain the answer, say "I couldn't find the answer in the provided context."
3. Do NOT use prior knowledge or search the web.
4. If quoting directly from the context, keep quotes brief.
5. Format the answer clearly. Use bullet points for lists if appropriate.
6. Mention the user who provided relevant information if their name is available in the context (e.g., "UserX mentioned...").
7. Do not invent information or speculate.
8. If multiple messages address the question, synthesize the information.
"""
        
        user_content = f"Question: {question}\n\nContext from Discord messages:\n{context}"
        messages = [{"role": "user", "content": user_content}]

        search_info['step_answer_generation_start'] = datetime.now()

        try:
            self.logger.info(f"Asking LLM (Claude Sonnet) to generate answer for: '{question[:50]}...'")
            # Call the dispatcher directly
            answer_text = await get_llm_response(
                client_name="claude",
                model="claude-sonnet-4-5-20250929",
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=1500 # Allow reasonable length for answer
            )
            
            self.logger.debug(f"LLM generated answer: {answer_text[:100]}...")
            search_info['step_answer_generation_llm_complete'] = datetime.now()
            
            search_info['generated_answer'] = answer_text # Store the generated answer
            search_info['step_answer_generation_complete'] = datetime.now()
            return answer_text
            
        except Exception as e:
            # Catch errors from the dispatcher
            self.logger.error(f"Error getting answer via LLM dispatcher: {e}", exc_info=True)
            search_info['step_answer_generation_error'] = str(e)
            search_info['step_answer_generation_complete'] = datetime.now()
            # Return a user-friendly error message
            return "Sorry, I encountered an error while generating the answer. Please try again later."

    async def create_answer_thread(self, channel_id: int, question_msg: discord.Message, answer: str, search_info: Dict):
        """Create a thread from the question message with the answer and search metadata."""
        try:
            # Create thread from the question message
            thread_name = f"Answer: {question_msg.content[:50]}..."
            # Ensure thread name length is within Discord limits (100 chars)
            if len(thread_name) > 100:
                thread_name = thread_name[:97] + "..."
                
            thread = await question_msg.create_thread(
                name=thread_name,
                auto_archive_duration=1440 # 24 hours
            )
            
            # --- Build Status/Metadata Message --- 
            # Initial status part
            status_lines = ["🔍 **Searching...**"] 
            
            # Selected Channels
            selected_channel_names = [f"• #{self.channel_map.get(cid, str(cid))}" 
                                      for cid in search_info.get('selected_channels', [])]
            if selected_channel_names:
                status_lines.append("\nChannels being searched:")
                status_lines.extend(selected_channel_names)
            else:
                status_lines.append("\nNo channels selected for search.")

            # Generated Queries
            generated_queries = search_info.get('generated_queries', [])
            if generated_queries:
                 status_lines.append("\nQueries being used:")
                 status_lines.extend([f"• `{q.get('query')}` ({q.get('reason')})" 
                                      for q in generated_queries])
            else:
                 status_lines.append("\nNo queries generated.")
            
            status_msg = "\n".join(status_lines)
            
            # Send initial status
            await thread.send(status_msg)
            
            # --- Build Final Metadata Message --- 
            metadata_lines = ["*Search completed with:*"]

            # Queries Used (redundant if in status, but maybe useful here too)
            if generated_queries:
                 metadata_lines.append(f"Queries: {', '.join(q.get('query') for q in generated_queries)}")
                 
            # Channel Results
            channel_results_summary = []
            for cid in search_info.get('selected_channels', []):
                channel_name = self.channel_map.get(cid, str(cid))
                result_count = len([msg for msg in search_info.get('results', []) 
                                  if msg.channel.id == cid])
                channel_results_summary.append(f"#{channel_name} ({result_count} results)")
            if channel_results_summary:
                 metadata_lines.append(f"Channels: {', '.join(channel_results_summary)}")
                 
            # Total Results
            total_results = len(search_info.get('results', []))
            metadata_lines.append(f"Total unique results: {total_results}")

            # Add Timings (calculate durations)
            metadata_lines.append("\n**Processing Times:**")
            t_start = search_info.get('step_received_question')
            t_chan_sel_llm = search_info.get('step_channel_selection_llm_complete')
            t_chan_sel = search_info.get('step_channel_selection_complete')
            t_query_gen_llm = search_info.get('step_query_generation_llm_complete')
            t_query_gen = search_info.get('step_query_generation_complete')
            t_search = search_info.get('step_search_complete')
            t_ans_gen_llm = search_info.get('step_answer_generation_llm_complete')
            t_ans_gen = search_info.get('step_answer_generation_complete')

            if t_start and t_chan_sel:
                 metadata_lines.append(f"- Channel Selection: {(t_chan_sel - t_start).total_seconds():.2f}s" + 
                                       (f" (LLM: {(t_chan_sel_llm - t_start).total_seconds():.2f}s)" if t_chan_sel_llm else ""))
            if t_chan_sel and t_query_gen:
                 metadata_lines.append(f"- Query Generation: {(t_query_gen - t_chan_sel).total_seconds():.2f}s" + 
                                       (f" (LLM: {(t_query_gen_llm - t_chan_sel).total_seconds():.2f}s)" if t_query_gen_llm else ""))
            if t_query_gen and t_search:
                 metadata_lines.append(f"- Discord Search: {(t_search - t_query_gen).total_seconds():.2f}s")
            if t_search and t_ans_gen:
                 metadata_lines.append(f"- Answer Generation: {(t_ans_gen - t_search).total_seconds():.2f}s" + 
                                       (f" (LLM: {(t_ans_gen_llm - t_search).total_seconds():.2f}s)" if t_ans_gen_llm else ""))
            if t_start and t_ans_gen: # Overall
                 metadata_lines.append(f"- Total Time: {(t_ans_gen - t_start).total_seconds():.2f}s")

            # Add Errors if they occurred
            errors = {k: v for k, v in search_info.items() if 'error' in k and v}
            if errors:
                 metadata_lines.append("\n**Errors Encountered:**")
                 for step, err_msg in errors.items():
                      metadata_lines.append(f"- {step.replace('step_','').replace('_error','')}: {str(err_msg)[:100]}...") # Truncate long errors
            
            metadata = "\n".join(metadata_lines)
            await thread.send(metadata)
            
            # Split answer into chunks if needed (Discord 2000 char limit)
            if not answer or not isinstance(answer, str):
                 self.logger.error(f"Invalid answer type received for thread: {type(answer)}")
                 answer = "[Error: Received invalid answer format from generation step]"
                 
            answer_chunks = [answer[i:i+1990] for i in range(0, len(answer), 1990)]
            for chunk in answer_chunks:
                await thread.send(chunk)
                
        except discord.errors.Forbidden:
             self.logger.error(f"Permission error creating thread in channel {channel_id}. Check bot permissions.")
             # Fallback: try to send as regular message
             channel = self.get_channel(channel_id)
             if channel:
                 try:
                     await channel.send(f"Error creating thread (Permissions Error). \n\nAnswer to {question_msg.author.mention}:\n{answer[:1900]}...") # Truncate answer
                 except Exception as fallback_e:
                     self.logger.error(f"Failed to send fallback message: {fallback_e}")
        except Exception as e:
            self.logger.error(f"Error creating answer thread: {e}", exc_info=True)
            # Fallback: try to send as regular message if thread creation fails
            channel = self.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(f"Error creating thread: {e}\n\nAnswer to {question_msg.author.mention}:\n{answer[:1900]}...") # Truncate answer
                except Exception as fallback_e:
                    self.logger.error(f"Failed to send fallback message: {fallback_e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handle incoming messages."""
        if message.channel.id != self.answer_channel_id or message.author.bot:
            return
        
        # Initialize search_info at the very start
        search_info = {
            'step_received_question': datetime.now(), # Record time immediately
            'question': message.content,
            'selected_channels': [],
            'generated_queries': [],
            'results': [],
            # Removed cost/token fields
        }
        
        # Add rate limiting check
        bucket = self.search_cooldown.get_bucket(message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await message.reply(f"Please wait {int(retry_after)} seconds before searching again.")
            return
            
        # Check if message is from admin user
        try:
             admin_user_id = int(os.getenv('ADMIN_USER_ID'))
        except (TypeError, ValueError):
             self.logger.error("ADMIN_USER_ID not found or invalid in environment variables.")
             await message.reply("Bot configuration error: Admin User ID not set.")
             return 
             
        if message.author.id != admin_user_id:
            self.logger.warning(f"Ignoring message from non-admin user: {message.author.id}")
            await message.reply(f"Sorry, I only run queries for the configured admin user.")
            return
            
        # Process the question
        question = message.content
        search_info['question'] = question # Ensure question is stored
        self.logger.info(f"\n--- New Question Received ---")
        self.logger.info(f"User: {message.author.name} ({message.author.id})")
        self.logger.info(f"Question: {question}")
        
        # Get all available channels
        self.channel_map = await self.get_searchable_channels()
        if not self.channel_map:
             await message.reply("Error: Could not retrieve searchable channels from the guild.")
             return
        
        # Step 1: Determine which channels to search
        relevant_channels = await self.determine_relevant_channels(question, self.channel_map, search_info)
        if not relevant_channels:
             await message.reply("Could not determine relevant channels to search. Please try rephrasing your question or check logs.")
             return # Stop if no channels selected
        search_info['selected_channels'] = relevant_channels # Ensure it's stored
        
        # Step 2: Generate search queries
        queries = await self.generate_search_queries(question, search_info)
        if not queries or not isinstance(queries[0].get("query"), str):
            await message.reply("Could not generate valid search queries. Please try rephrasing your question or check logs.")
            # Attempt to create a thread with the error info gathered so far
            await self.create_answer_thread(self.answer_channel_id, message, "[Error: Failed to generate valid search queries]", search_info)
            return
        search_info['generated_queries'] = queries # Ensure it's stored
        
        # Update user with initial status via thread (doing this early)
        # Create thread first, then send status inside
        thread = None
        try:
            thread_name = f"Answer: {question[:50]}..."
            if len(thread_name) > 100: thread_name = thread_name[:97] + "..."
            thread = await message.create_thread(name=thread_name, auto_archive_duration=1440)
            
            status_lines = ["🔍 **Searching...**"]
            selected_channel_names = [f"• #{self.channel_map.get(cid, str(cid))}" for cid in relevant_channels]
            if selected_channel_names: status_lines.extend(["\nChannels:", *selected_channel_names])
            query_lines = [f"• `{q.get('query')}` ({q.get('reason')})" for q in queries]
            if query_lines: status_lines.extend(["\nQueries:", *query_lines])
            await thread.send("\n".join(status_lines))
            
        except Exception as thread_e:
             self.logger.error(f"Failed to create initial thread or send status: {thread_e}")
             await message.reply(f"Error starting search thread: {thread_e}. Proceeding without thread updates.")
             # We can continue without the thread, but final answer posting needs care
             thread = None # Ensure thread is None if creation failed
        
        # Step 3: Collect all relevant messages
        search_info['step_search_start'] = datetime.now()
        all_results = []
        for query_dict in queries:
            query = query_dict.get('query')
            if not query or not isinstance(query, str):
                self.logger.warning(f"Skipping invalid query object: {query_dict}")
                continue
            self.logger.info(f"Executing search for query: '{query}'")
            # TODO: Implement search_channels method if needed, or rely on _search_recent_messages
            # results = await self.search_channels(query, relevant_channels)
            results = await self._search_recent_messages(query, relevant_channels)
            all_results.extend(results)
            self.logger.info(f"Found {len(results)} results for query '{query}'")
        
        # Remove duplicates while preserving order
        seen_ids = set()
        unique_results = []
        for msg in all_results:
            if msg.id not in seen_ids:
                seen_ids.add(msg.id)
                unique_results.append(msg)
        
        self.logger.info(f"Final unique results after combining queries: {len(unique_results)} messages")
        search_info['results'] = unique_results
        search_info['step_search_complete'] = datetime.now()
        
        # Step 4: Format context
        context = self.format_messages_for_context(unique_results)
        if not context:
             answer = "I found some relevant messages, but couldn't format them for context generation."
             self.logger.warning("Context formatting resulted in empty string.")
        else:
             # Step 5: Get answer from LLM
             answer = await self.get_claude_answer(question, context, search_info)
        
        # Step 6: Post answer (in thread if available, otherwise reply)
        if thread:
             # Send final metadata and answer chunks to the existing thread
             try:
                 metadata_lines = ["*Search completed with:*"]
                 # ... (build metadata lines as before, using search_info) ...
                 # Queries Used
                 if queries: metadata_lines.append(f"Queries: {', '.join(q.get('query') for q in queries)}")
                 # Channel Results
                 channel_results_summary = [f"#{self.channel_map.get(cid, str(cid))} ({len([m for m in unique_results if m.channel.id == cid])} results)" 
                                          for cid in relevant_channels]
                 if channel_results_summary: metadata_lines.append(f"Channels: {', '.join(channel_results_summary)}")
                 # Total Results
                 metadata_lines.append(f"Total unique results: {len(unique_results)}")
                 # Timings
                 metadata_lines.append("\n**Processing Times:**")
                 t_start = search_info.get('step_received_question')
                 t_chan_sel = search_info.get('step_channel_selection_complete')
                 t_query_gen = search_info.get('step_query_generation_complete')
                 t_search = search_info.get('step_search_complete')
                 t_ans_gen = search_info.get('step_answer_generation_complete')
                 if t_start and t_chan_sel: metadata_lines.append(f"- Channel Selection: {(t_chan_sel - t_start).total_seconds():.2f}s")
                 if t_chan_sel and t_query_gen: metadata_lines.append(f"- Query Generation: {(t_query_gen - t_chan_sel).total_seconds():.2f}s")
                 if t_query_gen and t_search: metadata_lines.append(f"- Discord Search: {(t_search - t_query_gen).total_seconds():.2f}s")
                 if t_search and t_ans_gen: metadata_lines.append(f"- Answer Generation: {(t_ans_gen - t_search).total_seconds():.2f}s")
                 if t_start and t_ans_gen: metadata_lines.append(f"- Total Time: {(t_ans_gen - t_start).total_seconds():.2f}s")
                 # Errors
                 errors = {k: v for k, v in search_info.items() if 'error' in k and v}
                 if errors: metadata_lines.extend(["\n**Errors Encountered:**", *[f"- {k}: {str(v)[:100]}..." for k, v in errors.items()]])
                 
                 await thread.send("\n".join(metadata_lines))
                 
                 # Send answer chunks
                 answer_chunks = [answer[i:i+1990] for i in range(0, len(answer), 1990)]
                 for chunk in answer_chunks:
                     await thread.send(chunk)
             except Exception as post_e:
                 self.logger.error(f"Error posting final answer/metadata to thread: {post_e}")
                 # Try a simple reply if thread posting fails
                 try: await message.reply(f"(Error posting to thread) Answer:\n{answer[:1900]}...")
                 except Exception: pass # Ignore reply error
        else:
             # Fallback reply if thread couldn't be created
             try:
                 reply_content = f"Answer to your question:\n{answer}"
                 reply_chunks = [reply_content[i:i+1990] for i in range(0, len(reply_content), 1990)]
                 for i, chunk in enumerate(reply_chunks):
                     if i == 0:
                         await message.reply(chunk)
                     else:
                         await message.channel.send(chunk) # Send subsequent chunks without reply
             except Exception as reply_e:
                 self.logger.error(f"Failed to send fallback reply: {reply_e}")

async def main():
    print("Starting main function...")
    try:
        bot = SearchAnswerBot()
        print("Bot instance created...")
        logging.info("Starting bot...")
        token = os.getenv('DISCORD_BOT_TOKEN')
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN not found in environment variables")
        print("Starting bot with token...")
        await bot.start(token)
    except Exception as e:
        print(f"Error starting bot: {e}")
        logging.error(f"Error starting bot: {e}")
        raise

if __name__ == "__main__":
    print("Script starting...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot shutdown by user")
        logging.info("\nBot shutdown by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        logging.error(f"Fatal error: {e}")
        logging.exception("Full traceback:")