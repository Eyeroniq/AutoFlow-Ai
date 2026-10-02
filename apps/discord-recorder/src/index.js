// The discord-recorder service: joins the monitored voice channel when the controller says to
// (app/discord_controller.py, after a Yes tapped in Telegram) and records everyone into one WAV.
//
// It decides nothing about *whether* to record. It reports what happens in the channel and does
// what it is told, through two Redis lists:
//   flowforge:discord:events    (this service -> the controller)
//     snapshot        {user_ids, channel_name}                 who is in the channel (on connect)
//     member_joined   {user_id, channel_name}
//     member_left     {user_id, channel_name}
//     recording_started {session}
//     recording_failed  {session, reason}
//     recording_ready   {session, path, seconds, speech_seconds, frames, has_audio, reason, channel_name}
//   flowforge:discord:commands  (the controller -> this service)
//     start_recording {session, max_minutes, output}
//     stop_recording  {reason}
// Voice receive and Discord's end-to-end encryption (DAVE) are @discordjs/voice's.

import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { EndBehaviorType, VoiceConnectionStatus, entersState, joinVoiceChannel } from '@discordjs/voice';
import { Client, GatewayIntentBits } from 'discord.js';
import OpusScript from 'opusscript';
import { createClient } from 'redis';

import { Mixer } from './mixer.js';

const EVENTS = 'flowforge:discord:events';
const COMMANDS = 'flowforge:discord:commands';
const CONSENT_NOTICE = '🔴 Recording started for meeting notes, per request';
const STOPPED_NOTICE = '⏹️ Recording ended. The audio recording will be sent to the person who approved it.';
const STOPPED_NOTICE_NOTES = '⏹️ Recording ended. A summary will be sent to the person who approved it.';

const token = (process.env.DISCORD_BOT_TOKEN ?? '').trim();
// The channel to watch comes from the Discord Voice Meeting block of a pipeline (the controller publishes it in
// Redis at WATCH); DISCORD_MONITOR_* in .env is the fallback when no pipeline has one.
const WATCH = 'flowforge:discord:watch';
const WATCH_POLL_MS = 5000;
let guildId = (process.env.DISCORD_MONITOR_GUILD_ID ?? '').trim();
let channelId = (process.env.DISCORD_MONITOR_CHANNEL_ID ?? '').trim();
const redisUrl = process.env.REDIS_URL ?? 'redis://localhost:6379/0';
const recordingsDir = process.env.DISCORD_RECORDINGS_DIR ?? '/data/files/recordings';

function log(level, message, extra = {}) {
  console.log(JSON.stringify({ timestamp: new Date().toISOString(), level, logger: 'discord-recorder', message, ...extra }));
}

if (!token) {
  log('WARNING', 'the Discord recorder is off; set DISCORD_BOT_TOKEN');
  setInterval(() => {}, 1 << 30); // idle, so the container doesn't restart in a loop
} else {
  await main();
}

async function main() {
  const redis = createClient({ url: redisUrl });
  const blocking = redis.duplicate(); // BLPOP holds its connection
  redis.on('error', (error) => log('ERROR', 'redis error', { error: String(error) }));
  blocking.on('error', (error) => log('ERROR', 'redis error', { error: String(error) }));
  await Promise.all([redis.connect(), blocking.connect()]);

  const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
  const humans = new Set(); // people in the monitored channel
  let recording = null;
  let stopping = false;

  const emit = (event) => redis.rPush(EVENTS, JSON.stringify({ ts: Date.now(), ...event }));
  const guild = () => client.guilds.cache.get(guildId);
  const channel = () => guild()?.channels.cache.get(channelId);
  let ready = false;

  // Watch whatever channel the controller publishes; re-snapshot who is in it when that changes.
  async function snapshot() {
    humans.clear();
    const target = channel();
    if (!target) {
      if (guildId || channelId) log('WARNING', 'the watched channel is not visible to the bot; check the ids and the invite', { guildId, channelId });
      return;
    }
    for (const state of guild().voiceStates.cache.values()) {
      if (state.channelId !== channelId) continue;
      const member = await guild().members.fetch(state.id).catch(() => null);
      if (member && !member.user.bot) humans.add(member.id);
    }
    await emit({ type: 'snapshot', user_ids: [...humans], channel_name: target.name });
    log('INFO', 'watching voice channel', { channel: target.name, already_present: humans.size });
  }

  async function pollWatch() {
    let raw = null;
    try {
      raw = await redis.get(WATCH);
    } catch (error) {
      log('ERROR', 'redis error', { error: String(error) });
      return;
    }
    let next = { guild_id: '', channel_id: '' };
    try {
      if (raw) next = JSON.parse(raw);
    } catch {
      return;
    }
    const g = String(next.guild_id ?? '').trim();
    const c = String(next.channel_id ?? '').trim();
    if (g === guildId && c === channelId) return;
    if (recording) return; // finish this meeting first
    guildId = g;
    channelId = c;
    if (ready) await snapshot();
  }

  // -- the channel ---------------------------------------------------------------------------------

  client.once('clientReady', async () => {
    ready = true;
    await pollWatch();
    await snapshot();
    setInterval(() => void pollWatch(), WATCH_POLL_MS);
  });

  client.on('voiceStateUpdate', async (before, after) => {
    const member = after.member ?? before.member;
    if (!ready || !guildId || !member || member.user.bot || after.guild.id !== guildId) return;
    const was = before.channelId === channelId;
    const now = after.channelId === channelId;
    if (was === now) return;
    const channel_name = channel()?.name ?? '';
    if (now) {
      humans.add(member.id);
      await emit({ type: 'member_joined', user_id: member.id, channel_name });
    } else {
      humans.delete(member.id);
      await emit({ type: 'member_left', user_id: member.id, channel_name });
      if (recording && humans.size === 0) await stopRecording('the channel is empty');
    }
  });

  // -- recording -----------------------------------------------------------------------------------

  async function startRecording(command) {
    if (recording) return;
    const target = channel();
    if (!target) {
      await emit({ type: 'recording_failed', session: command.session, reason: 'the watched channel is not visible to the bot' });
      return;
    }
    const session = command.session;
    let connection;
    try {
      connection = joinVoiceChannel({
        channelId, guildId, adapterCreator: guild().voiceAdapterCreator, selfDeaf: false, selfMute: true,
      });
      await entersState(connection, VoiceConnectionStatus.Ready, 30_000);
      // The visible notice comes first: no notice, no recording.
      await target.send(CONSENT_NOTICE);
    } catch (error) {
      connection?.destroy();
      log('ERROR', 'could not join or post the notice; not recording', { error: String(error) });
      await emit({ type: 'recording_failed', session, reason: `${error?.name ?? 'Error'}: ${error?.message ?? error}` });
      return;
    }

    const mixer = new Mixer();
    mixer.begin();
    const state = {
      session, connection, mixer, channelName: target.name, output: command.output ?? 'audio',
      decoders: new Map(), streams: new Map(), decodeErrors: 0, timer: null,
    };
    recording = state;
    const listen = (userId) => {
      if (userId === client.user.id || state.streams.has(userId)) return;
      const stream = connection.receiver.subscribe(userId, { end: { behavior: EndBehaviorType.Manual } });
      const decoder = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
      state.streams.set(userId, stream);
      state.decoders.set(userId, decoder);
      stream.on('data', (packet) => {
        try {
          mixer.add(userId, decoder.decode(packet));
        } catch {
          state.decodeErrors += 1;
        }
      });
      stream.on('error', (error) => log('WARNING', 'voice stream error', { error: String(error) }));
    };
    connection.receiver.speaking.on('start', listen);
    humans.forEach(listen);
    state.timer = setTimeout(() => stopRecording('max_duration'), (command.max_minutes ?? 90) * 60_000);
    connection.on('stateChange', (from, to) => log('INFO', 'voice connection', { from: from.status, to: to.status }));
    await emit({ type: 'recording_started', session });
    log('INFO', 'recording started', { channel: target.name });
  }

  async function stopRecording(reason) {
    const state = recording;
    if (!state) return;
    recording = null;
    clearTimeout(state.timer);
    state.mixer.end();
    for (const stream of state.streams.values()) stream.destroy();
    state.decoders.forEach((decoder) => decoder.delete?.());
    state.connection.destroy();
    const target = channel();
    await target?.send(state.output === 'notes' ? STOPPED_NOTICE_NOTES : STOPPED_NOTICE).catch(() => {});
    await mkdir(recordingsDir, { recursive: true });
    const file = path.join(recordingsDir, `${Date.now()}-${randomUUID()}.wav`);
    await writeFile(file, state.mixer.toWav());
    const info = {
      session: state.session, path: file, seconds: state.mixer.seconds, speech_seconds: state.mixer.speechSeconds,
      frames: state.mixer.frames, has_audio: state.mixer.hasAudio(), decode_errors: state.decodeErrors,
      reason, channel_name: state.channelName,
    };
    log('INFO', 'recording stopped', { ...info, path: undefined });
    await emit({ type: 'recording_ready', ...info });
  }

  // -- commands --------------------------------------------------------------------------------------

  async function commandLoop() {
    while (!stopping) {
      let item = null;
      try {
        item = await blocking.blPop(COMMANDS, 5);
      } catch (error) {
        if (stopping) return;
        log('ERROR', 'redis read failed', { error: String(error) });
        await new Promise((resolve) => setTimeout(resolve, 2000));
        continue;
      }
      if (!item) continue;
      try {
        const command = JSON.parse(item.element);
        if (command.type === 'start_recording') await startRecording(command);
        else if (command.type === 'stop_recording') await stopRecording(command.reason ?? 'stopped');
        else log('WARNING', 'unknown command', { type: command.type });
      } catch (error) {
        log('ERROR', 'command failed', { error: String(error?.stack ?? error) });
      }
    }
  }

  const shutdown = async () => {
    if (stopping) return;
    stopping = true;
    await stopRecording('the service is shutting down').catch(() => {});
    client.destroy();
    await Promise.allSettled([redis.quit(), blocking.disconnect()]);
    process.exit(0);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);

  await client.login(token);
  await commandLoop();
}
