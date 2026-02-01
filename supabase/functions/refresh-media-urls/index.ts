/**
 * Supabase Edge Function: Refresh Discord Media URLs
 *
 * Discord CDN attachment URLs expire after some time. This function:
 * 1. Takes a message_id (and optionally channel_id/thread_id)
 * 2. Looks up the message in the database to get channel info
 * 3. Fetches fresh URLs from Discord API
 * 4. Updates the database with the new URLs
 * 5. Returns the refreshed URLs
 *
 * Required secrets:
 *   - DISCORD_BOT_TOKEN: Your Discord bot token
 *
 * Usage:
 *   POST /functions/v1/refresh-media-urls
 *   Body (JSON):
 *     {
 *       "message_id": "123456789",           // Required: Discord message ID
 *       "channel_id": "987654321",           // Optional: override channel ID
 *       "thread_id": "111222333"             // Optional: thread ID for forum posts
 *     }
 *
 *   Response:
 *     {
 *       "success": true,
 *       "message_id": "123456789",
 *       "attachments": [...],                // Array of refreshed attachment objects
 *       "urls_updated": 2                    // Number of URLs that changed
 *     }
 */

import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

interface Attachment {
  id?: string;
  filename?: string;
  url?: string;
  proxy_url?: string;
  size?: number;
  content_type?: string;
  height?: number;
  width?: number;
}

interface DiscordAttachment {
  id: string;
  filename: string;
  url: string;
  proxy_url: string;
  size: number;
  content_type?: string;
  height?: number;
  width?: number;
}

interface DiscordMessage {
  id: string;
  channel_id: string;
  attachments: DiscordAttachment[];
}

// Fetch message from Discord API
async function fetchDiscordMessage(
  token: string,
  channelId: string,
  messageId: string
): Promise<DiscordMessage | null> {
  const url = `https://discord.com/api/v10/channels/${channelId}/messages/${messageId}`;
  console.log(`[fetchDiscordMessage] Fetching: ${url}`);

  const response = await fetch(url, {
    headers: {
      Authorization: `Bot ${token}`,
      "Content-Type": "application/json",
    },
  });

  console.log(`[fetchDiscordMessage] Response status: ${response.status}`);

  if (!response.ok) {
    const errorText = await response.text();
    console.log(`[fetchDiscordMessage] Error response: ${errorText}`);
    if (response.status === 404) {
      return null;
    }
    throw new Error(
      `Discord API error ${response.status}: ${response.statusText} - ${errorText}`
    );
  }

  return (await response.json()) as DiscordMessage;
}

// Main handler
Deno.serve(async (req) => {
  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    // Get Discord token from secrets
    const discordToken = Deno.env.get("DISCORD_BOT_TOKEN");
    if (!discordToken) {
      return new Response(
        JSON.stringify({ success: false, error: "DISCORD_BOT_TOKEN not configured" }),
        {
          status: 500,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }

    // Initialize Supabase client with service role (for DB updates)
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Parse request body
    const rawBody = await req.text();
    console.log(`[refresh-media-urls] Raw request body: ${rawBody}`);
    
    let body: Record<string, unknown> = {};
    try {
      body = JSON.parse(rawBody);
    } catch (e) {
      return new Response(
        JSON.stringify({ success: false, error: "Invalid JSON body" }),
        {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }
    
    // Convert to string to preserve precision for big integers
    const messageId = body.message_id?.toString();
    let channelId = body.channel_id?.toString();
    let threadId = body.thread_id?.toString();

    if (!messageId) {
      return new Response(
        JSON.stringify({ success: false, error: "message_id is required" }),
        {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }

    console.log(`[refresh-media-urls] Starting for message_id: ${messageId} (type: ${typeof body.message_id})`);

    // Look up the message in the database to get channel info if not provided
    // Use RPC to cast BIGINTs to TEXT to avoid JavaScript precision loss
    const { data: dbMessage, error: dbError } = await supabase
      .rpc("get_message_for_refresh", { p_message_id: messageId })
      .single();

    console.log(`[refresh-media-urls] DB query result - data: ${JSON.stringify(dbMessage)}, error: ${JSON.stringify(dbError)}`);

    if (dbError || !dbMessage) {
      console.log(`[refresh-media-urls] Message not found in DB. Error: ${JSON.stringify(dbError)}`);
      return new Response(
        JSON.stringify({
          success: false,
          error: `Message ${messageId} not found in database`,
          db_error: dbError?.message || null,
        }),
        {
          status: 404,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }

    // Use DB values if not provided in request (convert to string for Discord API)
    channelId = channelId || dbMessage.channel_id?.toString();
    threadId = threadId || dbMessage.thread_id?.toString();
    
    console.log(`[refresh-media-urls] Using channel_id: ${channelId}, thread_id: ${threadId}`);

    // Parse existing attachments
    let oldAttachments: Attachment[] = [];
    if (dbMessage.attachments) {
      if (typeof dbMessage.attachments === "string") {
        try {
          oldAttachments = JSON.parse(dbMessage.attachments);
        } catch {
          oldAttachments = [];
        }
      } else if (Array.isArray(dbMessage.attachments)) {
        oldAttachments = dbMessage.attachments;
      }
    }

    if (oldAttachments.length === 0) {
      return new Response(
        JSON.stringify({
          success: true,
          message_id: messageId,
          attachments: [],
          urls_updated: 0,
          note: "Message has no attachments in database",
        }),
        {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }

    // Try fetching from Discord - first try channel_id
    console.log(`[refresh-media-urls] Fetching from Discord - channel_id: ${channelId}, message_id: ${messageId}`);
    let discordMessage = await fetchDiscordMessage(
      discordToken,
      channelId,
      messageId
    );

    // If that fails and we have a thread_id, try that (for forum posts)
    if (!discordMessage && threadId) {
      console.log(`[refresh-media-urls] Retrying with thread_id: ${threadId}`);
      discordMessage = await fetchDiscordMessage(
        discordToken,
        threadId,
        messageId
      );
    }

    if (!discordMessage) {
      console.log(`[refresh-media-urls] Discord returned null for message ${messageId}`);
      return new Response(
        JSON.stringify({
          success: false,
          error: "Could not fetch message from Discord. It may have been deleted.",
          message_id: messageId,
          channel_id: channelId,
          thread_id: threadId || null,
        }),
        {
          status: 404,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }
    
    console.log(`[refresh-media-urls] Discord returned ${discordMessage.attachments?.length || 0} attachments`);

    // Build new attachments array, preserving structure
    const newAttachments: Attachment[] = discordMessage.attachments.map(
      (att) => ({
        id: att.id,
        filename: att.filename,
        url: att.url,
        proxy_url: att.proxy_url,
        size: att.size,
        content_type: att.content_type,
        height: att.height,
        width: att.width,
      })
    );

    if (newAttachments.length === 0) {
      return new Response(
        JSON.stringify({
          success: true,
          message_id: messageId,
          attachments: [],
          urls_updated: 0,
          note: "Message no longer has attachments on Discord",
        }),
        {
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }

    // Count how many URLs changed
    const oldUrlSet = new Set(oldAttachments.map((a) => a.url).filter(Boolean));
    const newUrlSet = new Set(newAttachments.map((a) => a.url).filter(Boolean));
    let urlsChanged = 0;
    for (const url of newUrlSet) {
      if (!oldUrlSet.has(url)) {
        urlsChanged++;
      }
    }

    // Update the database with new attachments
    const { error: updateError } = await supabase
      .from("discord_messages")
      .update({ attachments: newAttachments })
      .eq("message_id", messageId);

    if (updateError) {
      return new Response(
        JSON.stringify({
          success: false,
          error: `Database update failed: ${updateError.message}`,
          message_id: messageId,
        }),
        {
          status: 500,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        }
      );
    }

    console.log(
      `Updated message ${messageId}: ${newAttachments.length} attachments, ${urlsChanged} URLs changed`
    );

    // Return success with the new URLs
    return new Response(
      JSON.stringify({
        success: true,
        message_id: messageId,
        attachments: newAttachments,
        urls_updated: urlsChanged,
      }),
      {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }
    );
  } catch (error) {
    console.error("Error:", error);
    return new Response(
      JSON.stringify({
        success: false,
        error: error instanceof Error ? error.message : "Unknown error",
      }),
      {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      }
    );
  }
});
