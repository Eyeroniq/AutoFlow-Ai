// Which nodes act on someone's personal account, and when a node needs its user to connect their own.
// Kept free of React so it can be unit tested. The server enforces the same rule (a visitor's run
// never receives the owner's Gmail, Discord, Telegram, Notion, or Airtable); this is the explanation
// shown on the node itself, before a failed Run.

import type { Integration } from "@/lib/types";

/** The integration (Integrations page / `/api/integrations/{provider}`) a node type acts through. */
export function providerForNode(nodeType: string): string | null {
  if (nodeType === "gmail" || nodeType === "gmail_read") return "gmail";
  if (nodeType === "telegram") return "telegram";
  if (nodeType === "discord_webhook") return "discord";
  if (nodeType.startsWith("notion_")) return "notion";
  if (nodeType.startsWith("airtable_")) return "airtable";
  return null;
}

/**
 * The integration this node still needs connected, or null if it is ready: it needs no account, is set
 * to the mock sender, or its provider is connected (with the user's own credential, or, outside the
 * public demo, the server's). While integrations are still loading nothing is claimed.
 */
export function missingConnection(
  nodeType: string,
  config: Record<string, unknown> | undefined,
  integrations: Integration[] | undefined,
): Integration | null {
  const provider = providerForNode(nodeType);
  if (!provider || !integrations) return null;
  if (config?.auth === "mock") return null;
  const integration = integrations.find((i) => i.provider === provider);
  if (!integration) return null;
  return integration.connected || integration.source === "server" ? null : integration;
}

export interface HowTo {
  title: string;
  steps: string[];
  /** Where to start, when the provider has a page for it. */
  link?: { label: string; href: string };
  note?: string;
}

/** Where to get each credential: the same instructions as the Integrations page, one click from the node. */
export const HOW_TO: Record<string, HowTo> = {
  gmail: {
    title: "Get a Google App Password",
    steps: [
      "Turn on 2-Step Verification for your Google account.",
      "Open the App passwords page and create one named FlowForge.",
      "Copy the 16-character password (spaces don't matter).",
      "Enter your Gmail address and that password below. It sends and reads mail as you.",
    ],
    link: { label: "myaccount.google.com/apppasswords", href: "https://myaccount.google.com/apppasswords" },
    note: "Reading mail also needs IMAP switched on in Gmail's settings.",
  },
  telegram: {
    title: "Create a Telegram bot",
    steps: [
      "In Telegram, message @BotFather and send /newbot; pick a name and a username.",
      "Copy the token it gives you (like 123456789:AAE...).",
      "Send your new bot a message, then open https://api.telegram.org/bot<token>/getUpdates and copy message.chat.id.",
      "Paste the token (and, if you like, that chat id as the default chat) below.",
    ],
    link: { label: "t.me/BotFather", href: "https://t.me/BotFather" },
  },
  discord: {
    title: "Make a Discord webhook",
    steps: [
      "In your own Discord server, open the channel's settings, then Integrations, then Webhooks.",
      "Choose New Webhook, name it, and pick the channel.",
      "Choose Copy Webhook URL and paste it below.",
    ],
    note: "A webhook posts to that one channel only. Keep the URL private: anyone with it can post there.",
  },
  notion: {
    title: "Create a Notion integration",
    steps: [
      "Open Notion's integrations page and choose New integration (an internal one, in your workspace).",
      "Copy the integration token (it starts with ntn_ or secret_).",
      "Open each database or page you want to use, choose the ... menu, then Connections, and add your integration.",
      "Paste the token below.",
    ],
    link: { label: "notion.so/profile/integrations", href: "https://www.notion.so/profile/integrations" },
  },
  airtable: {
    title: "Create an Airtable personal access token",
    steps: [
      "Open Airtable's token page and choose Create new token.",
      "Give it the scopes data.records:read and data.records:write, and add the base you want to use.",
      "Copy the token (it starts with pat) and paste it below.",
    ],
    link: { label: "airtable.com/create/tokens", href: "https://airtable.com/create/tokens" },
  },
};
