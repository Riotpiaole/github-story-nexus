"""LLM prompt-response cache — binary storage, two levels.

Key derivation
--------------
  key = md5(zlib.compress(prompt_utf8))  →  32-char hex string

Entry payload layout (little-endian)
--------------------------------------
  [4B: compressed_prompt_len]
  [4B: compressed_response_len]
  [compressed_prompt_bytes]       ← used for collision detection
  [compressed_response_bytes]

L1  LocalCache  — in-memory dict backed by a single binary map file.
L2  Redis       — raw binary entries, 60 s TTL.

Collision detection
-------------------
  A stored entry whose compressed_prompt differs from the incoming one means
  two different prompts produced the same MD5 key.  A WARNING is emitted and
  None is returned so the caller's subsequent set() overrides the stale entry.
"""

import hashlib
import logging
import struct
import zlib
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import redis as _redis

log = logging.getLogger(__name__)

_TTL_SECONDS = 60
_CACHE_SUBDIR = Path(".cache") / "llm_invoke_cache"
_CACHE_FILE   = "cache.bin"

# Entry header: two LE uint32 (compressed_prompt_len, compressed_response_len)
_ENTRY_HDR = struct.Struct("<II")

# Map file header: 4-byte magic + LE uint32 entry count
_MAP_MAGIC   = b"LLMC"
_MAP_HDR     = struct.Struct("<4sI")   # magic(4) + num_entries(4)
_KEY_SIZE    = 32                       # MD5 hex is always 32 ASCII bytes
_PAYLOAD_LEN = struct.Struct("<I")


# ── Key derivation ────────────────────────────────────────────────────────────

def make_cache_key(prompt: str) -> tuple[str, bytes]:
    """Return (md5_hex_key, zlib_compressed_prompt)."""
    compressed = zlib.compress(prompt.encode("utf-8"), level=6)
    return hashlib.md5(compressed).hexdigest(), compressed


# ── Entry payload serialisation ───────────────────────────────────────────────

def pack_entry(compressed_prompt: bytes, response: str) -> bytes:
    compressed_response = zlib.compress(response.encode("utf-8"), level=6)
    return (
        _ENTRY_HDR.pack(len(compressed_prompt), len(compressed_response))
        + compressed_prompt
        + compressed_response
    )


def unpack_entry(data: bytes) -> tuple[bytes, str]:
    """Return (compressed_prompt, response_str).  Raises ValueError on corrupt data."""
    if len(data) < _ENTRY_HDR.size:
        raise ValueError("entry too short")
    prompt_len, response_len = _ENTRY_HDR.unpack_from(data)
    offset = _ENTRY_HDR.size
    if len(data) < offset + prompt_len + response_len:
        raise ValueError("entry truncated")
    compressed_prompt   = data[offset : offset + prompt_len]
    compressed_response = data[offset + prompt_len : offset + prompt_len + response_len]
    return compressed_prompt, zlib.decompress(compressed_response).decode("utf-8")


# ── Map file serialisation ────────────────────────────────────────────────────

def _serialize_map(store: dict[str, bytes]) -> bytes:
    parts: list[bytes] = [_MAP_HDR.pack(_MAP_MAGIC, len(store))]
    for key, payload in store.items():
        parts.append(key.encode("ascii").ljust(_KEY_SIZE)[:_KEY_SIZE])
        parts.append(_PAYLOAD_LEN.pack(len(payload)))
        parts.append(payload)
    return b"".join(parts)


def _deserialize_map(data: bytes) -> dict[str, bytes]:
    if len(data) < _MAP_HDR.size:
        raise ValueError("map file too short")
    magic, num_entries = _MAP_HDR.unpack_from(data)
    if magic != _MAP_MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    store: dict[str, bytes] = {}
    offset = _MAP_HDR.size
    for _ in range(num_entries):
        key = data[offset : offset + _KEY_SIZE].decode("ascii").rstrip()
        offset += _KEY_SIZE
        (payload_len,) = _PAYLOAD_LEN.unpack_from(data, offset)
        offset += _PAYLOAD_LEN.size
        store[key] = data[offset : offset + payload_len]
        offset += payload_len
    return store


# ── L1: LocalCache — LRU-bounded in-memory store + persisted binary map file ──

_MAX_ENTRIES = 100
_MAX_BYTES   = 100 * 1024 * 1024  # 100 MB


class LocalCache:
    """LRU-bounded in-memory OrderedDict[key → payload] backed by a binary map file.

    Bounded by two independent limits — whichever is hit first triggers eviction:
    - _MAX_ENTRIES (100): entry count
    - _MAX_BYTES (100 MB): total compressed payload size

    Lookups move the hit to the MRU end (O(1)).
    Writes insert at MRU, then evict from the LRU end until both limits are satisfied.
    Collision detection and entry unpacking happen inside get/set.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: OrderedDict[str, bytes] = OrderedDict()
        self._total_bytes: int = 0
        self._load()

    def get(self, key: str, compressed: bytes) -> str | None:
        payload = self._store.get(key)
        if payload is None:
            return None
        self._store.move_to_end(key)
        try:
            stored_prompt, response = unpack_entry(payload)
            if stored_prompt != compressed:
                log.warning("LLM local cache collision key=%s — overriding stale entry", key)
                return None
            return response
        except Exception as exc:
            log.warning("LLM local cache unpack error key=%s: %s", key, exc)
            return None

    def set(self, key: str, compressed: bytes, response: str) -> None:
        payload = pack_entry(compressed, response)
        if key in self._store:
            self._total_bytes -= len(self._store[key])
        self._store[key] = payload
        self._store.move_to_end(key)
        self._total_bytes += len(payload)
        self._evict()
        self._flush()

    def _evict(self) -> None:
        while self._store and (len(self._store) > _MAX_ENTRIES or self._total_bytes > _MAX_BYTES):
            _, evicted = self._store.popitem(last=False)
            self._total_bytes -= len(evicted)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._store = OrderedDict(_deserialize_map(self._path.read_bytes()))
            self._total_bytes = sum(len(v) for v in self._store.values())
            log.debug("LLM local cache loaded %d entries from %s", len(self._store), self._path)
        except Exception as exc:
            log.warning("LLM local cache load error (%s): %s — starting empty", self._path, exc)
            self._store = OrderedDict()
            self._total_bytes = 0

    def _flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_bytes(_serialize_map(dict(self._store)))
        except Exception as exc:
            log.warning("LLM local cache flush error: %s", exc)


@lru_cache(maxsize=4)
def get_local_cache(cache_dir: Path | None = None) -> LocalCache:
    """Return the singleton LocalCache for *cache_dir* (process-wide per path)."""
    path = (cache_dir or (Path.cwd() / _CACHE_SUBDIR)) / _CACHE_FILE
    return LocalCache(path)


# ── L2: Redis cache (binary) ──────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _redis_client(url: str) -> _redis.Redis:
    return _redis.Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry_on_timeout=False,
    )


def redis_get(url: str, key: str, compressed: bytes) -> str | None:
    try:
        raw = _redis_client(url).get(key)
        if raw is None:
            return None
        stored_prompt, response = unpack_entry(raw)
        if stored_prompt != compressed:
            log.warning("LLM cache L2 hash collision key=%s — overriding stale entry", key)
            return None
        return response
    except Exception as exc:
        log.warning("LLM cache L2 GET failed key=%s: %s", key, exc)
        return None


def redis_set(url: str, key: str, compressed: bytes, response: str) -> None:
    try:
        _redis_client(url).set(key, pack_entry(compressed, response), ex=_TTL_SECONDS)
    except Exception as exc:
        log.warning("LLM cache L2 SET failed key=%s: %s", key, exc)
