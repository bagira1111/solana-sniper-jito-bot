import os
import json
import time
import asyncio
import base64
import uuid
import random
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager

import requests
import websockets
from dotenv import load_dotenv

from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction as SoldersVTx
from solders.transaction import Transaction as SoldersLegacyTx
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.system_program import transfer as sys_transfer, TransferParams

from urllib.parse import urlparse, urlunparse


# ==========================
# timing helpers
# ==========================

def now_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def tlog(name: str, timings: dict):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round((time.perf_counter() - t0) * 1000, 2)


# ==========================
# .env
# ==========================

ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)


def as_bool(v: Optional[str], default=False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def as_float(v: Optional[str], default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def as_int(v: Optional[str], default: int) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


# ==========================
# ENV CONFIG
# ==========================

RPC_URL = os.getenv("RPC_URL")
if not RPC_URL:
    raise RuntimeError("В .env не задан RPC_URL")


def _derive_ws_url_from_http(rpc_url: str) -> str:
    p = urlparse(rpc_url)
    if p.scheme in ("http", "https"):
        ws_scheme = "wss" if p.scheme == "https" else "ws"
    else:
        ws_scheme = "wss"
    return urlunparse((ws_scheme, p.netloc, p.path, p.params, p.query, p.fragment))


RPC_WS_URL = _derive_ws_url_from_http(RPC_URL)

PRIVATE_KEY_BASE58 = (os.getenv("PRIVATE_KEY_BASE58") or "").strip()
if not PRIVATE_KEY_BASE58:
    raise RuntimeError("В .env не задан PRIVATE_KEY_BASE58")

PUMP_API_KEY = (os.getenv("PUMP_API_KEY") or "").strip()
if not PUMP_API_KEY:
    raise RuntimeError("В .env не задан PUMP_API_KEY")

WATCH_WALLET_RAW = (os.getenv("WATCH_WALLET") or "").strip()
WATCH_WALLETS: List[str] = []
if WATCH_WALLET_RAW:
    parts = [p.strip() for p in WATCH_WALLET_RAW.split(",") if p.strip()]
    for p in parts:
        if p.startswith("WATCH_WALLET="):
            p = p.split("=", 1)[1].strip()
        if p:
            WATCH_WALLETS.append(p)

if not WATCH_WALLETS:
    raise RuntimeError("В .env не задан WATCH_WALLET (или пустой)")

MIN_WALLET_BUY_SOL = as_float(os.getenv("MIN_WALLET_BUY_SOL"), 0.01)
TRIGGER_SELL_SOL = as_float(os.getenv("TRIGGER_SELL_SOL"), 0.1)

# IMPORTANT: BUY_SOL тут используем как "BUY_WSOL" (сколько WSOL тратим на покупку)
BUY_SOL = as_float(os.getenv("BUY_SOL"), 0.01)
BUY_WSOL = BUY_SOL

SLIPPAGE_BPS = as_int(os.getenv("SLIPPAGE_BPS"), 900)

PRIORITY_FEE_LAMPORTS = as_int(os.getenv("PRIORITY_FEE_LAMPORTS"), 0)
SELL_PRIORITY_FEE_LAMPORTS = as_int(os.getenv("SELL_PRIORITY_FEE_LAMPORTS"), PRIORITY_FEE_LAMPORTS)

# TP/SL включаются через AUTO_SELL=true в .env
AUTO_SELL = as_bool(os.getenv("AUTO_SELL"), False)
TP_PCT = as_float(os.getenv("AUTO_TP_PCT"), 5.0)
SL_PCT = as_float(os.getenv("AUTO_SL_PCT"), 25.0)
POLL_SECONDS = as_float(os.getenv("AUTO_SELL_POLL_INTERVAL"), 1.0)

# ✅ чтобы не спамить ценой каждую итерацию autosell
AUTOSELL_LOG_EVERY_SEC = as_float(os.getenv("AUTOSELL_LOG_EVERY_SEC"), 10.0)

REQUIRE_PUMPFUN = as_bool(os.getenv("REQUIRE_PUMPFUN"), True)
POOL_AMM_ID = (os.getenv("POOL_AMM_ID") or "").strip() or None

JUP_BASE_ENV = (os.getenv("JUP_BASE") or "").strip()
JUP_BASES_ONLY = as_bool(os.getenv("JUP_BASES_ONLY"), True)

if JUP_BASES_ONLY:
    JUP_BASES = [JUP_BASE_ENV or "https://lite-api.jup.ag"]
else:
    JUP_BASES = ["https://lite-api.jup.ag", "https://api.jup.ag"]

if JUP_BASE_ENV:
    if JUP_BASE_ENV in JUP_BASES:
        JUP_BASES.remove(JUP_BASE_ENV)
    JUP_BASES.insert(0, JUP_BASE_ENV)

SKIP_PREFLIGHT = as_bool(os.getenv("SKIP_PREFLIGHT"), False)
FAST_CONFIRM = as_bool(os.getenv("FAST_CONFIRM"), True)

ONE_SELL_DROP_MIN_PCT = as_float(os.getenv("ONE_SELL_DROP_MIN_PCT"), 2.0)
ONE_SELL_DROP_MAX_PCT = as_float(os.getenv("ONE_SELL_DROP_MAX_PCT"), 70.0)

MIN_LIQ_USD = as_float(os.getenv("MIN_LIQ_USD"), 40_000.0)
MIN_SELL_SHARE_PCT = as_float(os.getenv("MIN_SELL_SHARE_PCT"), 1.0)
SOL_PRICE_USD = as_float(os.getenv("SOL_PRICE_USD"), 150.0)

# LIQ cache behavior
LIQ_TTL_SEC = as_float(os.getenv("LIQ_TTL_SEC"), 25.0)  # кэш валиден N секунд
LIQ_REFRESH_SEC = as_float(os.getenv("LIQ_REFRESH_SEC"), 12.0)  # фоновый refresh раз в N секунд
LIQ_REFRESH_CONCURRENCY = as_int(os.getenv("LIQ_REFRESH_CONCURRENCY"), 4)

WSOL_MINT = "So11111111111111111111111111111111111111112"

# IMPORTANT:
# - Pump.fun токены часто Token-2022
# - WSOL (So111...) — legacy SPL Token
TOKEN_PROGRAM_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
TOKEN_PROGRAM_LEGACY = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# ==========================
# Jito sender + UUID + TIP
# ==========================

USE_JITO_SEND = as_bool(os.getenv("USE_JITO_SEND"), True)
JITO_FALLBACK_RPC = as_bool(os.getenv("JITO_FALLBACK_RPC"), True)

JITO_UUID = (os.getenv("JITO_UUID") or "").strip()
if not JITO_UUID:
    JITO_UUID = str(uuid.uuid4())

JITO_ENGINE_URL = (os.getenv("JITO_ENGINE_URL") or "https://mainnet.block-engine.jito.wtf").strip()
JITO_TX_URL = JITO_ENGINE_URL.rstrip("/") + "/api/v1/transactions"
JITO_BUNDLE_URL = JITO_ENGINE_URL.rstrip("/") + "/api/v1/bundles"

JITO_TIP_LAMPORTS = as_int(os.getenv("JITO_TIP_LAMPORTS"), 0)
JITO_TIP_ACCOUNTS_ENV = (os.getenv("JITO_TIP_ACCOUNTS") or "").strip()
JITO_TIP_ACCOUNTS: List[str] = []
if JITO_TIP_ACCOUNTS_ENV:
    for x in JITO_TIP_ACCOUNTS_ENV.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            Pubkey.from_string(x)
            JITO_TIP_ACCOUNTS.append(x)
        except Exception:
            pass

JITO_TIP_CONFIRM = as_bool(os.getenv("JITO_TIP_CONFIRM"), False)

# WSOL balance via WS subscription toggle
WSOL_WS_BALANCE = as_bool(os.getenv("WSOL_WS_BALANCE"), True)

# ✅ DEBUG маршрутов (убирает DEBUG QUOTE/ENTRY)
DEBUG_ROUTES = as_bool(os.getenv("DEBUG_ROUTES"), False)

print(f"[KEY CFG] RPC_URL={RPC_URL}")
print(f"[KEY CFG] RPC_WS_URL={RPC_WS_URL}")
print(f"[CFG] WATCH_WALLETS={','.join(WATCH_WALLETS)} MIN_WALLET_BUY_SOL={MIN_WALLET_BUY_SOL} TRIGGER_SELL_SOL={TRIGGER_SELL_SOL}")
print(f"[CFG] BUY_WSOL={BUY_WSOL} SLIPPAGE_BPS={SLIPPAGE_BPS} PRIO_BUY={PRIORITY_FEE_LAMPORTS} PRIO_SELL={SELL_PRIORITY_FEE_LAMPORTS}")
print(f"[CFG] AUTO_SELL={AUTO_SELL} TP={TP_PCT}% SL={SL_PCT}% POLL={POLL_SECONDS}s AUTOSELL_LOG_EVERY_SEC={AUTOSELL_LOG_EVERY_SEC}s")
print(f"[CFG] JUP_BASES={JUP_BASES} REQUIRE_PUMPFUN={REQUIRE_PUMPFUN} POOL_AMM_ID={POOL_AMM_ID}")
print(f"[CFG] MIN_LIQ_USD={MIN_LIQ_USD} MIN_SELL_SHARE_PCT={MIN_SELL_SHARE_PCT} SOL_PRICE_USD≈{SOL_PRICE_USD}")
print(f"[CFG] LIQ_TTL_SEC={LIQ_TTL_SEC} LIQ_REFRESH_SEC={LIQ_REFRESH_SEC} LIQ_REFRESH_CONCURRENCY={LIQ_REFRESH_CONCURRENCY}")
print(f"[CFG] DEBUG_ROUTES={DEBUG_ROUTES}")
print(f"[JITO] USE_JITO_SEND={USE_JITO_SEND} JITO_ENGINE_URL={JITO_ENGINE_URL} JITO_UUID={JITO_UUID}")
print(f"[JITO TIP] tip_lamports={JITO_TIP_LAMPORTS} tip_accounts_env={len(JITO_TIP_ACCOUNTS)} confirm={JITO_TIP_CONFIRM}")
print(f"[WSOL-WS] enabled={WSOL_WS_BALANCE}")


# ==========================
# KEYPAIR
# ==========================

def load_keypair() -> SoldersKeypair:
    try:
        kp = SoldersKeypair.from_base58_string(PRIVATE_KEY_BASE58)
        print("[KEY] Загружен PRIVATE_KEY_BASE58")
        return kp
    except Exception as e:
        raise RuntimeError(f"Не удалось разобрать PRIVATE_KEY_BASE58: {e}")


KP = load_keypair()
MY_PUBKEY = str(KP.pubkey())
print(f"[KEY] Паблик бота: {MY_PUBKEY}")


# ==========================
# watched_tokens.json + positions.json
# ==========================

TOKENS_FILE = Path(__file__).with_name("watched_tokens.json")
POSITIONS_FILE = Path(__file__).with_name("positions.json")

WATCH_LOCK = asyncio.Lock()  # защита WATCHED_MINTS + SUBSCRIBED_TOKENS


def load_watched_mints() -> List[str]:
    if not TOKENS_FILE.exists():
        print("[TOKENS] watched_tokens.json не найден (начинаем с нуля).")
        return []
    try:
        data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        tokens = data.get("tokens") or []
        return [str(m).strip() for m in tokens if str(m).strip()]
    except json.JSONDecodeError:
        print("[TOKENS] watched_tokens.json повреждён или пустой, игнорирую содержимое.")
        return []
    except Exception as e:
        print("[TOKENS] Ошибка при чтении watched_tokens.json:", repr(e))
        return []


def save_watched_mints(mints: List[str]) -> None:
    mset = sorted(set(mints))
    TOKENS_FILE.write_text(json.dumps({"tokens": mset}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[TOKENS] Сохранено {len(mset)} токенов в watched_tokens.json")


WATCHED_MINTS: List[str] = load_watched_mints()
print(
    f"[TOKENS] Загружено {len(WATCHED_MINTS)} токенов из watched_tokens.json"
    if WATCHED_MINTS
    else "[TOKENS] Список токенов пуст."
)
SUBSCRIBED_TOKENS: set[str] = set(WATCHED_MINTS)


async def add_watched_mint_async(mint: str):
    """Добавление mint в watchlist + сохранение на диск — НЕ блокирует WS loop."""
    async with WATCH_LOCK:
        if mint in WATCHED_MINTS:
            return
        WATCHED_MINTS.append(mint)
        mints_snapshot = list(WATCHED_MINTS)
    await asyncio.to_thread(save_watched_mints, mints_snapshot)
    print(f"[TOKENS] Добавлен mint в watch-лист: {mint}")


# ==========================
# PRICE CACHE + MINT PROGRAM CACHE
# ==========================

TOKEN_DECIMALS_CACHE: Dict[str, int] = {}

# mint -> "legacy" | "2022"
MINT_TOKEN_PROGRAM_CACHE: Dict[str, Pubkey] = {}
MINT_TOKEN_PROGRAM_LOCK = asyncio.Lock()

# LIQ_CACHE[mint] = (liq_usd, sol_in_pool_est, ts_epoch)
LIQ_CACHE: Dict[str, Tuple[float, float, float]] = {}
LIQ_LOCK = asyncio.Lock()

JUP_RATE_LIMIT_UNTIL: float = 0.0


def jup_in_cooldown() -> bool:
    return time.time() < JUP_RATE_LIMIT_UNTIL


def jup_set_cooldown(sec: float = 10.0):
    global JUP_RATE_LIMIT_UNTIL
    JUP_RATE_LIMIT_UNTIL = max(JUP_RATE_LIMIT_UNTIL, time.time() + sec)


# ==========================
# HTTP / RPC
# ==========================

JUP_SESSION = requests.Session()
JUP_SESSION.headers.update(
    {
        "origin": "https://jup.ag",
        "referer": "https://jup.ag/",
        "user-agent": "Mozilla/5.0",
        "accept": "application/json",
    }
)

RPC_SESSION = requests.Session()
DEX_SESSION = requests.Session()

JITO_SESSION = requests.Session()
JITO_SESSION.headers.update(
    {
        "content-type": "application/json",
        "x-jito-auth": JITO_UUID,
    }
)


def rpc_call(method: str, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = RPC_SESSION.post(RPC_URL, json=payload, timeout=(3, 30))
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"RPC {method} error: {j['error']}")
    return j["result"]


def http_get_with_fallback(path: str, params: dict, timeout=10, retries=3, backoff=0.4):
    last_err = None
    for base in JUP_BASES:
        url = base.rstrip("/") + path
        for attempt in range(retries):
            try:
                r = JUP_SESSION.get(url, params=params, timeout=(3, timeout))
                if r.status_code in (401, 404):
                    last_err = requests.HTTPError(f"{r.status_code} @ {url}", response=r)
                    time.sleep(backoff * (attempt + 1))
                    continue
                return r
            except Exception as e:
                last_err = e
                time.sleep(backoff * (attempt + 1))
    raise requests.exceptions.ConnectionError(
        f"Не удалось обратиться к Jupiter. Базы: {JUP_BASES}. Последняя ошибка: {last_err}"
    )


def http_post_with_fallback(path: str, json_body: dict, timeout=25, retries=2, backoff=0.5):
    last_err = None
    for base in JUP_BASES:
        url = base.rstrip("/") + path
        for attempt in range(retries):
            try:
                r = JUP_SESSION.post(url, json=json_body, timeout=(3, timeout))
                if r.status_code in (401, 404):
                    last_err = requests.HTTPError(f"{r.status_code} @ {url}", response=r)
                    time.sleep(backoff * (attempt + 1))
                    continue
                return r
            except Exception as e:
                last_err = e
                time.sleep(backoff * (attempt + 1))
    raise requests.exceptions.ConnectionError(
        f"Не удалось выполнить swap в Jupiter. Базы: {JUP_BASES}. Последняя ошибка: {last_err}"
    )


# ==========================
# MINT -> TOKEN PROGRAM (legacy vs 2022)
# ==========================

def get_mint_token_program_sync(mint: str) -> Pubkey:
    """
    Определяет token program mint'а по owner mint-аккаунта.
    - Tokenkeg... -> legacy SPL Token
    - TokenzQd... -> Token-2022
    """
    if mint in MINT_TOKEN_PROGRAM_CACHE:
        return MINT_TOKEN_PROGRAM_CACHE[mint]

    res = rpc_call("getAccountInfo", [mint, {"encoding": "base64", "commitment": "processed"}])
    value = res.get("value")
    if not value:
        # если mint не существует/пусто — по умолчанию legacy, чтобы не падать
        prog = TOKEN_PROGRAM_LEGACY
        MINT_TOKEN_PROGRAM_CACHE[mint] = prog
        return prog

    owner = (value.get("owner") or "").strip()
    if owner == str(TOKEN_PROGRAM_2022):
        prog = TOKEN_PROGRAM_2022
    else:
        # большинство — legacy
        prog = TOKEN_PROGRAM_LEGACY

    MINT_TOKEN_PROGRAM_CACHE[mint] = prog
    return prog


async def get_mint_token_program(mint: str) -> Pubkey:
    # кэш + lock, чтобы не дергать RPC многократно при пике
    if mint in MINT_TOKEN_PROGRAM_CACHE:
        return MINT_TOKEN_PROGRAM_CACHE[mint]
    async with MINT_TOKEN_PROGRAM_LOCK:
        if mint in MINT_TOKEN_PROGRAM_CACHE:
            return MINT_TOKEN_PROGRAM_CACHE[mint]
        prog = await asyncio.to_thread(get_mint_token_program_sync, mint)
        MINT_TOKEN_PROGRAM_CACHE[mint] = prog
        return prog


def get_ata_address(mint: str, owner: str, token_program: Pubkey) -> str:
    mint_pk = Pubkey.from_string(mint)
    owner_pk = Pubkey.from_string(owner)
    ata_pk, _ = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(token_program), bytes(mint_pk)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return str(ata_pk)


# ==========================
# LIQUIDITY (Dexscreener)
# ==========================

def get_liquidity_info(mint: str) -> Optional[Tuple[float, float]]:
    """
    Возвращает (liq_usd, sol_in_pool_est) или None при 429/ошибке.
    Важно: None не должно попадать в кэш, иначе ты "закэшируешь нули".
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        r = DEX_SESSION.get(url, timeout=7)

        if r.status_code == 429:
            print(f"[DEX LIQ] 429 rate limit (skip) mint={mint}")
            return None

        r.raise_for_status()
        j = r.json()
        pairs = j.get("pairs") or []
        if not pairs:
            return (0.0, 0.0)

        best_liq = None
        for p in pairs:
            chain = (p.get("chainId") or "").lower()
            if chain not in ("solana", "sol"):
                continue
            liq = (p.get("liquidity") or {}).get("usd")
            if liq is None:
                continue
            try:
                liq_f = float(liq)
            except Exception:
                continue
            if best_liq is None or liq_f > best_liq:
                best_liq = liq_f

        if best_liq is None:
            return (0.0, 0.0)

        sol_in_pool_est = best_liq / (2.0 * SOL_PRICE_USD) if SOL_PRICE_USD > 0 else 0.0
        return (float(best_liq), float(sol_in_pool_est))

    except Exception as e:
        print(f"[DEX LIQ] Ошибка Dexscreener для {mint}: {e}")
        return None


async def get_liquidity_cached_fast(mint: str) -> Tuple[float, float, bool]:
    """Возвращает (liq, sol_est, from_cache)."""
    now = time.time()
    async with LIQ_LOCK:
        if mint in LIQ_CACHE:
            liq, sol_est, ts = LIQ_CACHE[mint]
            if (now - ts) <= LIQ_TTL_SEC:
                return liq, sol_est, True

    res = await asyncio.to_thread(get_liquidity_info, mint)

    # 429/ошибка: верни старое, а если его нет — 0, но НЕ кэшируй
    if res is None:
        async with LIQ_LOCK:
            if mint in LIQ_CACHE:
                liq, sol_est, ts = LIQ_CACHE[mint]
                return liq, sol_est, True
        return 0.0, 0.0, False

    liq, sol_est = res
    async with LIQ_LOCK:
        LIQ_CACHE[mint] = (liq, sol_est, time.time())
    return liq, sol_est, False


async def liquidity_refresher():
    """Фоновое обновление ликвидности."""
    sem = asyncio.Semaphore(max(1, LIQ_REFRESH_CONCURRENCY))

    async def refresh_one(m: str):
        async with sem:
            res = await asyncio.to_thread(get_liquidity_info, m)
            if res is None:
                return  # не кэшируем "ошибки/429"
            liq, sol_est = res
            async with LIQ_LOCK:
                LIQ_CACHE[m] = (liq, sol_est, time.time())

    while True:
        try:
            async with WATCH_LOCK:
                mints = list(set(WATCHED_MINTS) | set(SUBSCRIBED_TOKENS))
            if mints:
                tasks = [asyncio.create_task(refresh_one(m)) for m in mints]
                await asyncio.wait(tasks, timeout=10.0)
            await asyncio.sleep(max(2.0, LIQ_REFRESH_SEC))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2.0)


# ==========================
# Jupiter
# ==========================

def _debug_print_route(obj: dict, tag: str):
    if not DEBUG_ROUTES:
        return
    rp = obj.get("routePlan") or []
    print(f"DEBUG {tag}: hops={len(rp)}")
    for i, hop in enumerate(rp, 1):
        info = hop.get("swapInfo") or {}
        print(
            f"  #{i} label={info.get('label')} ammKey={info.get('ammKey')} "
            f"in={info.get('inAmount')} out={info.get('outAmount')}"
        )


def _filter_routes_pumpfun_strict(obj: dict, require_pump: bool, pool_amm_id: Optional[str]) -> dict:
    if not require_pump:
        return obj

    rp = obj.get("routePlan") or []
    for hop in rp:
        info = hop.get("swapInfo") or {}
        label = str(info.get("label", "")).lower()
        amm_key = (info.get("ammKey") or "").strip()
        if pool_amm_id:
            if amm_key == pool_amm_id:
                return obj
        else:
            if any(k in label for k in ("pump.fun", "pumpfun", "pump")):
                return obj

    raise RuntimeError(
        "Маршрут через Pump.fun не найден" + (f" (требовался пул {pool_amm_id})" if pool_amm_id else "")
    )


def jup_quote_pump_only(
    input_mint: str,
    output_mint: str,
    amount_smallest: int,
    require_pump: Optional[bool] = None,
    pool_amm_id: Optional[str] = None,
) -> dict:
    if require_pump is None:
        require_pump = REQUIRE_PUMPFUN
    if pool_amm_id is None:
        pool_amm_id = POOL_AMM_ID

    base = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_smallest),
        "slippageBps": SLIPPAGE_BPS,
    }

    variants = [
        {**base},
        {**base, "onlyDirectRoutes": "true"},
        {**base, "dexes": "pump"},
        {**base, "dexes": ["pump", "pump.fun"]},
        {**base, "excludeDexes": "meteora"},
        {**base, "excludeDexes": ["meteora"]},
    ]

    last_err: Optional[Exception] = None
    for idx, params in enumerate(variants, 1):
        try:
            r = http_get_with_fallback("/swap/v1/quote", params=params, timeout=10)
            r.raise_for_status()
            obj = r.json()
            _debug_print_route(obj, f"QUOTE v{idx}")
            return _filter_routes_pumpfun_strict(obj, require_pump, pool_amm_id)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                raise RuntimeError("Jupiter 429 Too Many Requests") from e
            last_err = e
        except Exception as e:
            last_err = e

    raise RuntimeError(f"QUOTE_FAILED: {last_err}")


def jup_quote_for_entry(
    input_mint: str,
    output_mint: str,
    amount_smallest: int,
    require_pump: Optional[bool] = None,
    pool_amm_id: Optional[str] = None,
) -> dict:
    if require_pump is None:
        require_pump = False
    if pool_amm_id is None:
        pool_amm_id = None

    base = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_smallest),
        "slippageBps": SLIPPAGE_BPS,
    }

    variants = [
        {**base},
        {**base, "onlyDirectRoutes": "true"},
        {**base, "dexes": "pump"},
        {**base, "dexes": ["pump", "pump.fun"]},
        {**base, "excludeDexes": "meteora"},
    ]

    last_err: Optional[Exception] = None
    for idx, params in enumerate(variants, 1):
        try:
            r = http_get_with_fallback("/swap/v1/quote", params=params, timeout=8)
            r.raise_for_status()
            obj = r.json()
            _debug_print_route(obj, f"ENTRY v{idx}")
            return _filter_routes_pumpfun_strict(obj, require_pump, pool_amm_id)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                raise RuntimeError("Jupiter 429 Too Many Requests") from e
            last_err = e
        except Exception as e:
            last_err = e

    raise RuntimeError(f"ENTRY_QUOTE_FAILED: {last_err}")


def jup_swap(route_obj: dict, user_pubkey: str, priority_fee_lamports: Optional[int] = None) -> str:
    if priority_fee_lamports is None:
        priority_fee_lamports = PRIORITY_FEE_LAMPORTS

    body = {
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": False,
        "useSharedAccounts": True,
        "useTokenLedger": False,
        "dynamicComputeUnitLimit": True,
        "dynamicSlippage": True,
        "prioritizationFeeLamports": priority_fee_lamports,
        "quoteResponse": route_obj,
    }

    r = http_post_with_fallback("/swap/v1/swap", json_body=body, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Jupiter swap HTTP {r.status_code}: {r.text}")

    data = r.json()
    tx_b64 = data.get("swapTransaction")
    if not tx_b64:
        raise RuntimeError(f"Jupiter swap не вернул swapTransaction: {data}")
    return tx_b64


# ==========================
# BALANCES
# ==========================

def get_mint_decimals(mint: str) -> int:
    res = rpc_call("getTokenSupply", [mint])
    val = res.get("value") or {}
    return int(val.get("decimals", 9))


def get_mint_decimals_cached(mint: str) -> int:
    if mint in TOKEN_DECIMALS_CACHE:
        return TOKEN_DECIMALS_CACHE[mint]
    d = get_mint_decimals(mint)
    TOKEN_DECIMALS_CACHE[mint] = d
    return d


def get_token_balance_raw(mint: str, owner: str) -> tuple[int, int]:
    """
    Возвращает баланс по mint у owner.
    Важно: getTokenAccountsByOwner с фильтром mint вернёт токен-аккаунты,
    независимо от token program (legacy/2022). Мы берём ПЕРВЫЙ — это ок
    для большинства кейсов, но на всякий случай можно заменить на max по amount.
    """
    res = rpc_call(
        "getTokenAccountsByOwner",
        [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "processed"},
        ],
    )
    value = res.get("value") or []
    if not value:
        return 0, get_mint_decimals(mint)

    acc = value[0]
    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
    ta = info.get("tokenAmount", {}) or {}
    amount = int(ta.get("amount", "0"))
    decimals = int(ta.get("decimals", 9))
    return amount, decimals


# --- WSOL: ATA всегда через legacy program ---
WSOL_ATA: Optional[str] = None
WSOL_ACCOUNT: Optional[str] = None  # реальный токен-аккаунт с WSOL (может быть НЕ ATA)

def find_best_wsol_account(owner: str) -> Optional[str]:
    """Находит лучший (самый большой по amount) WSOL token account у owner."""
    try:
        res = rpc_call(
            "getTokenAccountsByOwner",
            [owner, {"mint": WSOL_MINT}, {"encoding": "jsonParsed", "commitment": "processed"}],
        )
        best_pk = None
        best_amt = -1
        for it in (res.get("value") or []):
            pk = it.get("pubkey")
            info = it.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            ta = info.get("tokenAmount") or {}
            amt = int(ta.get("amount", "0") or "0")
            if amt > best_amt:
                best_amt = amt
                best_pk = pk
        return best_pk
    except Exception:
        return None


try:
    WSOL_ATA = get_ata_address(WSOL_MINT, MY_PUBKEY, TOKEN_PROGRAM_LEGACY)
    print(f"[WSOL] ATA(legacy) = {WSOL_ATA}")
except Exception as e:
    print(f"[WSOL] Не удалось посчитать ATA(legacy): {e}")
    WSOL_ATA = None


def get_wsol_balance_lamports(owner: str) -> int:
    bal_raw, _ = get_token_balance_raw(WSOL_MINT, owner)
    return int(bal_raw)


def get_wsol_balance_fast(owner: str) -> int:
    """Быстрый WSOL баланс:
    1) если найден реальный WSOL_ACCOUNT — читаем его
    2) иначе пробуем WSOL_ATA (legacy)
    3) иначе фолбэк на getTokenAccountsByOwner
    """
    global WSOL_ACCOUNT, WSOL_ATA

    if WSOL_ACCOUNT:
        try:
            res = rpc_call("getTokenAccountBalance", [WSOL_ACCOUNT, {"commitment": "processed"}])
            val = res.get("value") or {}
            return int(val.get("amount", "0") or "0")
        except Exception:
            pass

    if WSOL_ATA:
        try:
            res = rpc_call("getTokenAccountBalance", [WSOL_ATA, {"commitment": "processed"}])
            val = res.get("value") or {}
            return int(val.get("amount", "0") or "0")
        except Exception:
            pass

    return get_wsol_balance_lamports(owner)


# --- WS подписка на WSOL account (0 RPC запросов в hot-path) ---
WSOL_BALANCE_LAMPORTS: int = 0
WSOL_BALANCE_READY: bool = False


async def wsol_balance_ws_listener():
    global WSOL_BALANCE_LAMPORTS, WSOL_BALANCE_READY

    if not WSOL_WS_BALANCE:
        print("[WSOL-WS] выключено (WSOL_WS_BALANCE=false).")
        return

    target = WSOL_ACCOUNT or WSOL_ATA
    if not target:
        print("[WSOL-WS] нет WSOL_ACCOUNT/WSOL_ATA, подписка невозможна.")
        return

    backoff = 1.0
    while True:
        try:
            print(f"[WSOL-WS] subscribe {target} @ {RPC_WS_URL}")
            async with websockets.connect(RPC_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "accountSubscribe",
                            "params": [target, {"encoding": "jsonParsed", "commitment": "processed"}],
                        }
                    )
                )

                # initial
                try:
                    WSOL_BALANCE_LAMPORTS = int(get_wsol_balance_fast(MY_PUBKEY))
                    WSOL_BALANCE_READY = True
                except Exception:
                    pass

                backoff = 1.0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("method") != "accountNotification":
                        continue

                    info = (
                        (msg.get("params") or {})
                        .get("result", {})
                        .get("value", {})
                        .get("data", {})
                        .get("parsed", {})
                        .get("info", {})
                        .get("tokenAmount", {})
                    )

                    try:
                        WSOL_BALANCE_LAMPORTS = int(info.get("amount", "0") or "0")
                        WSOL_BALANCE_READY = True
                    except Exception:
                        continue

        except asyncio.CancelledError:
            raise
        except Exception as e:
            WSOL_BALANCE_READY = False
            print(f"[WSOL-WS] ошибка: {e} (reconnect in {backoff:.1f}s)")
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)


async def wait_for_token_via_ws(mint: str, owner: str, timeout: float = 30.0) -> tuple[int, int]:
    # ATA считаем по РЕАЛЬНОМУ token program этого mint
    prog = await get_mint_token_program(mint)
    ata = get_ata_address(mint, owner, prog)
    print(f"[WS-ATA] mint={mint} program={'2022' if prog == TOKEN_PROGRAM_2022 else 'legacy'} ATA={ata}")

    bal_raw, dec = await asyncio.to_thread(get_token_balance_raw, mint, owner)
    if bal_raw > 0:
        print(f"[WS-ATA] Уже есть баланс токена (HTTP): raw={bal_raw}")
        return bal_raw, dec

    print(f"[WS-ATA] Подписываюсь по RPC WS {RPC_WS_URL} на accountSubscribe {ata}")
    try:
        async with websockets.connect(RPC_WS_URL, ping_interval=20, ping_timeout=20) as ws:
            sub = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "accountSubscribe",
                "params": [ata, {"encoding": "jsonParsed", "commitment": "processed"}],
            }
            await ws.send(json.dumps(sub))
            start_ts = time.time()
            while True:
                if timeout and (time.time() - start_ts) > timeout:
                    print(f"[WS-ATA] Таймаут по {ata}, фолбэк на polling.")
                    break

                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("method") != "accountNotification":
                    continue

                params = msg.get("params") or {}
                result = params.get("result") or {}
                value = result.get("value") or {}
                data = value.get("data") or {}
                parsed = data.get("parsed") or {}
                info = parsed.get("info") or {}
                ta = info.get("tokenAmount") or {}
                amount_str = ta.get("amount", "0")
                decimals = int(ta.get("decimals", 9))
                try:
                    amount = int(amount_str)
                except Exception:
                    amount = 0

                print(f"[WS-ATA] accountNotification {ata}: amount={amount}, decimals={decimals}")
                if amount > 0:
                    print(f"[WS-ATA] Токен {mint} появился на кошельке, raw={amount}")
                    return amount, decimals
    except Exception as e:
        print(f"[WS-ATA] Ошибка WS accountSubscribe {ata}: {e}")

    print("[WS-ATA] Переход к HTTP polling баланса…")
    while True:
        bal_raw, dec = await asyncio.to_thread(get_token_balance_raw, mint, owner)
        if bal_raw > 0:
            print(f"[WS-ATA] Токен обнаружен polling: raw={bal_raw}")
            return bal_raw, dec
        await asyncio.sleep(POLL_SECONDS)


# ==========================
# JITO TIP ACCOUNTS
# ==========================

def jito_get_tip_accounts() -> List[str]:
    if JITO_TIP_ACCOUNTS:
        return JITO_TIP_ACCOUNTS

    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
    r = JITO_SESSION.post(JITO_BUNDLE_URL, json=payload, timeout=(2, 6))
    if r.status_code != 200:
        raise RuntimeError(f"getTipAccounts HTTP {r.status_code}: {r.text}")

    j = r.json()
    if "error" in j:
        raise RuntimeError(f"getTipAccounts error: {j['error']}")

    res = j.get("result")
    if not isinstance(res, list):
        raise RuntimeError(f"getTipAccounts unexpected result: {j}")

    valid: List[str] = []
    for s in res:
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not s:
            continue
        try:
            Pubkey.from_string(s)
            valid.append(s)
        except Exception:
            continue

    if not valid:
        raise RuntimeError("getTipAccounts вернул 0 валидных pubkey (проверь endpoint или задай JITO_TIP_ACCOUNTS).")

    return valid


def pick_jito_tip_account() -> str:
    tips = jito_get_tip_accounts()
    return random.choice(tips)


def _get_recent_blockhash() -> Hash:
    res = rpc_call("getLatestBlockhash", [{"commitment": "processed"}])
    bh = (res.get("value") or {}).get("blockhash")
    if not bh:
        raise RuntimeError(f"getLatestBlockhash без blockhash: {res}")
    return Hash.from_string(bh)


def build_tip_tx_b64_signed(tip_lamports: int) -> str:
    tip_to = pick_jito_tip_account()
    to_pk = Pubkey.from_string(tip_to)
    payer = KP.pubkey()

    ix = sys_transfer(TransferParams(from_pubkey=payer, to_pubkey=to_pk, lamports=tip_lamports))
    recent = _get_recent_blockhash()

    tx = SoldersLegacyTx.new_signed_with_payer([ix], payer, [KP], recent)

    try:
        wire = bytes(tx)
    except Exception:
        wire = tx.to_bytes()

    return base64.b64encode(wire).decode()


# ==========================
# SEND TX
# ==========================

def _sign_swap_tx(base64_tx: str) -> str:
    raw = base64.b64decode(base64_tx)
    tx = SoldersVTx.from_bytes(raw)
    tx_signed = SoldersVTx(tx.message, [KP])
    try:
        wire = bytes(tx_signed)
    except Exception:
        wire = tx_signed.to_bytes()
    return base64.b64encode(wire).decode()


def _send_via_jito(base64_signed_tx: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [base64_signed_tx, {"encoding": "base64"}],
    }

    url = f"{JITO_TX_URL}?uuid={JITO_UUID}"
    r = JITO_SESSION.post(url, json=payload, timeout=(2, 8))
    if r.status_code != 200:
        raise RuntimeError(f"Jito HTTP {r.status_code}: {r.text}")

    j = r.json()
    if "error" in j:
        raise RuntimeError(f"Jito sendTransaction error: {j['error']}")
    sig = j.get("result")
    if not sig:
        raise RuntimeError(f"Jito sendTransaction: нет result в ответе: {j}")

    print(f"⚡ SENT VIA JITO/SHREDSTREAM: {sig}")
    return sig


def _confirm_signature(sig_str: str) -> str:
    deadline = time.time() + 40
    last_status = None

    while time.time() < deadline:
        statuses = rpc_call("getSignatureStatuses", [[sig_str], {"searchTransactionHistory": True}])
        stat = (statuses.get("value") or [None])[0]
        last_status = stat

        if not stat:
            time.sleep(0.4)
            continue

        conf = stat.get("confirmationStatus")
        err = stat.get("err")

        if FAST_CONFIRM:
            if conf in ("processed", "confirmed", "finalized"):
                if err is None:
                    print(f"✅ success ({conf}) {sig_str}")
                    return sig_str
                raise RuntimeError(f"Transaction {sig_str} failed on-chain: {err}")
        else:
            if conf in ("confirmed", "finalized"):
                if err is None:
                    print(f"✅ success ({conf}) {sig_str}")
                    return sig_str
                raise RuntimeError(f"Transaction {sig_str} failed on-chain: {err}")

        time.sleep(0.4)

    raise RuntimeError(f"Timeout подтверждения. Последний статус: {last_status}")


# --- главный loop для fire-and-forget confirm из worker thread ---
MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


async def confirm_in_background(sig: str):
    try:
        await asyncio.to_thread(_confirm_signature, sig)
    except Exception as e:
        print(f"[CONFIRM BG] tx {sig} failed: {e}")


def _schedule_confirm(sig: str):
    """Надёжно планирует confirm на главном event loop, даже если вызвали из to_thread."""
    global MAIN_LOOP
    try:
        loop = MAIN_LOOP
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(confirm_in_background(sig), loop)
    except Exception as e:
        print(f"[CONFIRM BG] schedule failed for {sig}: {e}")


def maybe_send_jito_tip_best_effort():
    if not USE_JITO_SEND or JITO_TIP_LAMPORTS <= 0:
        return
    try:
        tip_b64 = build_tip_tx_b64_signed(JITO_TIP_LAMPORTS)
        sig = _send_via_jito(tip_b64)
        print(f"[JITO TIP] sent tip tx sig={sig} lamports={JITO_TIP_LAMPORTS}")
        if JITO_TIP_CONFIRM:
            _confirm_signature(sig)
    except Exception as e:
        print(f"[JITO TIP] tip send failed (ignored): {e}")


def send_signed(base64_tx_from_jup: str) -> str:
    """Fire-and-forget: возвращает sig сразу после отправки. confirm уезжает в фон."""
    t0 = time.perf_counter()

    maybe_send_jito_tip_best_effort()

    t_sign = time.perf_counter()
    b64_signed = _sign_swap_tx(base64_tx_from_jup)
    sign_ms = round((time.perf_counter() - t_sign) * 1000, 2)

    if USE_JITO_SEND:
        try:
            t_send = time.perf_counter()
            sig = _send_via_jito(b64_signed)
            send_ms = round((time.perf_counter() - t_send) * 1000, 2)
            total_ms = round((time.perf_counter() - t0) * 1000, 2)
            print(f"[SPEED/SEND] sign={sign_ms}ms send={send_ms}ms total={total_ms}ms (fire-and-forget JITO)")
            _schedule_confirm(sig)
            return sig
        except Exception as e:
            print(f"[JITO] Ошибка отправки через Jito (нет sig): {e}")
            if not JITO_FALLBACK_RPC:
                raise
            print("[JITO] Fallback → обычный RPC sendTransaction")

    send_opts = {
        "encoding": "base64",
        "skipPreflight": SKIP_PREFLIGHT,
        "preflightCommitment": "processed" if FAST_CONFIRM else "confirmed",
    }

    t_send = time.perf_counter()
    sig_str = rpc_call("sendTransaction", [b64_signed, send_opts])
    send_ms = round((time.perf_counter() - t_send) * 1000, 2)
    total_ms = round((time.perf_counter() - t0) * 1000, 2)
    print(f"[SPEED/SEND] sign={sign_ms}ms send={send_ms}ms total={total_ms}ms (fire-and-forget RPC)")
    print("⛓ sent via RPC:", sig_str)

    _schedule_confirm(sig_str)
    return sig_str


# ==========================
# ENTRY / AUTOSELL
# ==========================

@dataclass
class EntryState:
    mint: str
    entry_price_wsol_per_token: float
    amm_key: Optional[str]
    decimals: int
    active: bool = True


ACTIVE_ENTRIES: Dict[str, EntryState] = {}

# anti-double-buy: mint'ы, по которым покупка уже запущена, но позиция ещё не добавлена в ACTIVE_ENTRIES
PENDING_ENTRIES: set[str] = set()
PENDING_LOCK = asyncio.Lock()

ENTRY_SEM = asyncio.Semaphore(1)  # чтобы не шло 10 покупок одновременно


def save_positions_from_active() -> None:
    try:
        payload: Dict[str, Any] = {"positions": {}}
        for mint, entry in ACTIVE_ENTRIES.items():
            if not entry.active:
                continue
            payload["positions"][mint] = {
                "entry_price_wsol_per_token": entry.entry_price_wsol_per_token,
                "amm_key": entry.amm_key,
                "decimals": entry.decimals,
                "active": entry.active,
            }
        POSITIONS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[POS] Сохранено {len(payload['positions'])} активных позиций в {POSITIONS_FILE.name}")
    except Exception as e:
        print(f"[POS] Ошибка сохранения позиций: {e}")


def restore_autosell_from_disk() -> None:
    if not AUTO_SELL:
        print("[POS] AUTO_SELL выключен, сохранённые позиции игнорируются.")
        return

    if not POSITIONS_FILE.exists():
        print("[POS] positions.json не найден, активных позиций нет.")
        return

    try:
        data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
        positions: Dict[str, Any] = data.get("positions") or {}
        if not positions:
            print("[POS] В positions.json нет активных позиций.")
            return

        for mint, p in positions.items():
            if not p.get("active", True):
                continue

            try:
                entry_price = float(p.get("entry_price_wsol_per_token") or 0.0)
            except Exception:
                entry_price = 0.0
            if entry_price <= 0:
                continue

            amm_key = (p.get("amm_key") or None) or None
            try:
                decimals = int(p.get("decimals", 6))
            except Exception:
                decimals = 6

            state = EntryState(
                mint=mint,
                entry_price_wsol_per_token=entry_price,
                amm_key=amm_key,
                decimals=decimals,
                active=True,
            )
            ACTIVE_ENTRIES[mint] = state
            asyncio.create_task(autosell_worker(state))
            print(f"[POS] Восстановлена позиция по {mint}, entry≈{entry_price:.10f} WSOL/токен")
    except Exception as e:
        print(f"[POS] Ошибка при восстановлении позиций: {e}")


async def autosell_worker(entry: EntryState):
    mint = entry.mint
    print(
        f"[AUTOSELL] Старт по {mint}. Входная: {entry.entry_price_wsol_per_token:.10f} WSOL/токен, "
        f"TP=+{TP_PCT:.1f}%, SL=-{SL_PCT:.1f}%"
    )

    bal_raw, dec = await wait_for_token_via_ws(mint, MY_PUBKEY)
    entry.decimals = dec
    print(f"[AUTOSELL] Обнаружен баланс токена: raw={bal_raw}, decimals={dec}")

    target_up = 1.0 + TP_PCT / 100.0
    target_down = 1.0 - SL_PCT / 100.0

    last_log = 0.0

    while entry.active:
        bal_raw, dec = await asyncio.to_thread(get_token_balance_raw, mint, MY_PUBKEY)
        if bal_raw <= 0:
            print(f"[AUTOSELL] Баланс {mint} = 0, выхожу.")
            entry.active = False
            ACTIVE_ENTRIES.pop(mint, None)
            save_positions_from_active()
            break

        amount_tokens = bal_raw / (10 ** dec)

        timings = {}
        try:
            with tlog("quote_ms", timings):
                quote = await asyncio.to_thread(jup_quote_pump_only, mint, WSOL_MINT, bal_raw, True, entry.amm_key)
        except Exception as e:
            msg = str(e)
            if "429" in msg:
                print("[AUTOSELL] Jupiter 429, пауза 8с...")
                await asyncio.sleep(8.0)
            else:
                print(f"[AUTOSELL] Ошибка quote: {e}")
                await asyncio.sleep(POLL_SECONDS)
            continue

        out_lamports = int(quote.get("outAmount", "0") or "0")
        if out_lamports <= 0:
            await asyncio.sleep(POLL_SECONDS)
            continue

        est_wsol = out_lamports / 1_000_000_000
        price = est_wsol / amount_tokens
        ratio = price / entry.entry_price_wsol_per_token

        # ✅ меньше спама: лог раз в AUTOSELL_LOG_EVERY_SEC секунд
        now_t = time.time()
        if AUTOSELL_LOG_EVERY_SEC <= 0:
            pass
        elif (now_t - last_log) >= AUTOSELL_LOG_EVERY_SEC:
            print(f"[AUTOSELL] price={price:.10f} WSOL/токен ratio={ratio:.4f} quote_ms={timings.get('quote_ms')}ms")
            last_log = now_t

        do_sell = False
        if ratio >= target_up:
            print(f"[AUTOSELL] 🎯 TAKE PROFIT ({ratio:.2f}x) — продаю всё.")
            do_sell = True
        elif ratio <= target_down:
            print(f"[AUTOSELL] 🛑 STOP LOSS ({ratio:.2f}x) — продаю всё.")
            do_sell = True

        if not do_sell:
            await asyncio.sleep(POLL_SECONDS)
            continue

        try:
            timings2 = {}
            with tlog("quote_sell_ms", timings2):
                quote_sell = await asyncio.to_thread(jup_quote_pump_only, mint, WSOL_MINT, bal_raw, True, entry.amm_key)
            with tlog("swap_build_ms", timings2):
                b64_tx = await asyncio.to_thread(jup_swap, quote_sell, MY_PUBKEY, SELL_PRIORITY_FEE_LAMPORTS)
            with tlog("send_total_ms", timings2):
                sig = await asyncio.to_thread(send_signed, b64_tx)

            print(
                f"[AUTOSELL] SELL sig={sig} quote={timings2.get('quote_sell_ms')}ms "
                f"build={timings2.get('swap_build_ms')}ms send={timings2.get('send_total_ms')}ms"
            )

            entry.active = False
            ACTIVE_ENTRIES.pop(mint, None)
            save_positions_from_active()
        except Exception as e:
            print(f"[AUTOSELL] Ошибка SELL: {e}")
            await asyncio.sleep(POLL_SECONDS)

    print(f"[AUTOSELL] Завершён для {mint}")


async def enter_token_on_signal(
    mint: str,
    sol_amt: float,
    sell_share_pct: float,
    drop_pct: float,
    ws_recv_ms_stamp: Optional[int] = None,
):
    async with ENTRY_SEM:
        try:
            # защита от гонок: если позиция уже активна — ничего не делаем
            st = ACTIVE_ENTRIES.get(mint)
            if st is not None and st.active:
                return

            timings = {"ws_recv_ms": ws_recv_ms_stamp or now_ms()}
            t_total = time.perf_counter()

            wsol_in_lamports = int(BUY_WSOL * 1_000_000_000)

            with tlog("wsol_balance_ms", timings):
                if WSOL_WS_BALANCE and WSOL_BALANCE_READY:
                    cur_wsol = int(WSOL_BALANCE_LAMPORTS)
                else:
                    cur_wsol = await asyncio.to_thread(get_wsol_balance_fast, MY_PUBKEY)

            if cur_wsol < wsol_in_lamports:
                need = (wsol_in_lamports - cur_wsol) / 1_000_000_000
                print(
                    f"[ENTRY/{mint}] ❌ Недостаточно WSOL. Баланс={cur_wsol/1e9:.6f} WSOL, "
                    f"нужно={BUY_WSOL:.6f} WSOL (не хватает {need:.6f} WSOL). Скип."
                )
                return

            print(
                f"🔥 [TOKEN {mint}] Сигнал входа: продажа {sol_amt:.4f} SOL (share≈{sell_share_pct:.2f}%), "
                f"покупаю на {BUY_WSOL} WSOL..."
            )

            try:
                with tlog("quote_ms", timings):
                    quote = await asyncio.to_thread(jup_quote_for_entry, WSOL_MINT, mint, wsol_in_lamports, False, None)
            except Exception as e:
                msg = str(e)
                if "429" in msg:
                    print(f"[ENTRY/{mint}] Jupiter 429 на quote, скип.")
                    return
                print(f"[ENTRY/{mint}] Ошибка quote: {e}")
                return

            out_raw = int(quote.get("outAmount", "0") or "0")
            if out_raw <= 0:
                print(f"[ENTRY/{mint}] outAmount=0, отменяю.")
                return

            with tlog("decimals_ms", timings):
                dec = await asyncio.to_thread(get_mint_decimals_cached, mint)

            amount_tokens = out_raw / (10 ** dec)
            if amount_tokens <= 0:
                print(f"[ENTRY/{mint}] amount_tokens<=0, отменяю.")
                return

            wsol_in = wsol_in_lamports / 1_000_000_000
            entry_price_wsol_per_token = wsol_in / amount_tokens

            amm_key = None
            label = None
            rp = quote.get("routePlan") or []
            if rp:
                info = rp[0].get("swapInfo") or {}
                amm_key = (info.get("ammKey") or "").strip()
                label = info.get("label")

            print(f"[ENTRY/{mint}] entry≈{entry_price_wsol_per_token:.10f} WSOL/токен (~{amount_tokens:.4f} токенов)")
            if label or amm_key:
                print(f"✅ Маршрут: {label}, пул: {amm_key}")

            try:
                with tlog("swap_build_ms", timings):
                    b64_tx = await asyncio.to_thread(jup_swap, quote, MY_PUBKEY, PRIORITY_FEE_LAMPORTS)
                with tlog("send_total_ms", timings):
                    sig = await asyncio.to_thread(send_signed, b64_tx)
            except Exception as e:
                print(f"[ENTRY/{mint}] Ошибка отправки: {e}")
                return

            timings["TOTAL_ms"] = round((time.perf_counter() - t_total) * 1000, 2)
            timings["ws_to_entry_ms"] = round((now_ms() - timings["ws_recv_ms"]), 2)

            print(
                f"[SPEED/{mint}] ws_to_entry={timings.get('ws_to_entry_ms')}ms "
                f"wsol_balance={timings.get('wsol_balance_ms')}ms "
                f"quote={timings.get('quote_ms')}ms swap_build={timings.get('swap_build_ms')}ms "
                f"send_total={timings.get('send_total_ms')}ms TOTAL={timings.get('TOTAL_ms')}ms sig={sig}"
            )

            print(f"[ENTRY/{mint}] Покупка отправлена, sig: {sig}")

            # ✅ фикс: не покупать повторно "пока не продали"
            state = EntryState(
                mint=mint,
                entry_price_wsol_per_token=entry_price_wsol_per_token,
                amm_key=amm_key,
                decimals=dec,
                active=True,
            )
            ACTIVE_ENTRIES[mint] = state
            save_positions_from_active()

            if AUTO_SELL:
                asyncio.create_task(autosell_worker(state))
            else:
                print(f"[ENTRY/{mint}] AUTO_SELL выключен — позиция помечена активной (повторных покупок не будет).")

        finally:
            # pending снимаем всегда
            async with PENDING_LOCK:
                PENDING_ENTRIES.discard(mint)


# ==========================
# PUMPPORTAL WS
# ==========================

PUMP_WSS_URL = f"wss://pumpportal.fun/api/data?api-key={PUMP_API_KEY}"


def _get_side_flags(msg: dict) -> Tuple[bool, bool]:
    is_buy_raw = msg.get("is_buy")
    if is_buy_raw is None:
        is_buy_raw = msg.get("isBuy")

    if isinstance(is_buy_raw, bool):
        return bool(is_buy_raw), (not bool(is_buy_raw))

    side_raw = str(msg.get("side") or msg.get("tradeType") or msg.get("txType") or "").lower()
    if side_raw == "buy":
        return True, False
    if side_raw == "sell":
        return False, True

    return False, False


async def handle_trade_msg(msg: dict, ws):
    timings = {"ws_recv_ms": now_ms()}
    t0 = time.perf_counter()

    if "mint" not in msg and "token" not in msg and "tokenMint" not in msg and "tokenAddress" not in msg:
        return

    mint = msg.get("mint") or msg.get("token") or msg.get("tokenMint") or msg.get("tokenAddress")
    if not mint:
        return

    possible_trader_keys = [
        "wallet",
        "buyer",
        "seller",
        "account",
        "owner",
        "user",
        "userPubkey",
        "user_pubkey",
        "traderPublicKey",
        "trader_public_key",
        "from",
        "to",
    ]
    trader = None
    for k in possible_trader_keys:
        val = msg.get(k)
        if isinstance(val, str) and len(val) > 10:
            trader = val
            break

    if trader is None:
        trader = msg.get("wallet") or msg.get("account") or msg.get("owner") or msg.get("trader") or msg.get("user")

    seller = trader or "UNKNOWN"

    is_buy, is_sell = _get_side_flags(msg)
    if not is_buy and not is_sell:
        return

    sol_amt = 0.0
    for k in ("sol_amount", "solAmount", "native", "nativeAmount"):
        if k in msg and msg[k] is not None:
            try:
                sol_amt = float(msg[k])
                break
            except Exception:
                continue

    # --- wallet tracking (без блокировки)
    if trader in WATCH_WALLETS and is_buy and sol_amt >= MIN_WALLET_BUY_SOL:
        print(f"[WALLET-BUY] {trader} купил {mint} на {sol_amt:.4f} SOL")
        asyncio.create_task(add_watched_mint_async(mint))
        async with WATCH_LOCK:
            need_sub = mint not in SUBSCRIBED_TOKENS
            if need_sub:
                SUBSCRIBED_TOKENS.add(mint)
        if need_sub:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
            print(f"[WS] subscribed to token: {mint}")

    async with WATCH_LOCK:
        watched = mint in WATCHED_MINTS
    if not watched:
        return

    if not is_sell:
        return
    if sol_amt <= 0:
        return

    # ✅ ТИХО: если уже есть позиция (или уже идёт вход) — игнорим без liq/логов
    st = ACTIVE_ENTRIES.get(mint)
    if st is not None and st.active:
        return
    async with PENDING_LOCK:
        if mint in PENDING_ENTRIES:
            return

    # ---- LIQ check ----
    with tlog("liq_ms", timings):
        cur_liq, sol_in_pool_est, from_cache = await get_liquidity_cached_fast(mint)

    # если Dexscreener отдал 429/ошибку и ликва не известна — не спамим "0.00"
    if cur_liq <= 0:
        return

    if cur_liq < MIN_LIQ_USD:
        print(
            f"[TOKEN {mint}] Ликвидность низкая (${cur_liq:.2f} < ${MIN_LIQ_USD:.2f}), скип. "
            f"liq_ms={timings.get('liq_ms')}ms cache={from_cache}"
        )
        return

    sell_share_pct = 0.0
    if sol_in_pool_est > 0:
        sell_share_pct = 100.0 * sol_amt / sol_in_pool_est

    if sol_in_pool_est > 0 and sell_share_pct < MIN_SELL_SHARE_PCT:
        print(
            f"[TOKEN {mint}] Продажа мала: {sell_share_pct:.2f}% пула (< {MIN_SELL_SHARE_PCT}%), скип. "
            f"liq_ms={timings.get('liq_ms')}ms cache={from_cache}"
        )
        return

    timings["after_filters_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    print(
        f"[TOKEN {mint}] ПРОДАЖА на {sol_amt:.4f} SOL от {seller}. liq≈${cur_liq:.0f} share≈{sell_share_pct:.2f}% "
        f"(after_filters={timings.get('after_filters_ms')}ms liq_ms={timings.get('liq_ms')}ms cache={from_cache})"
    )

    # ✅ anti-double-buy: помечаем pending уже тут (после фильтров)
    async with PENDING_LOCK:
        if mint in PENDING_ENTRIES:
            return
        PENDING_ENTRIES.add(mint)

    asyncio.create_task(
        enter_token_on_signal(
            mint,
            sol_amt=sol_amt,
            sell_share_pct=sell_share_pct,
            drop_pct=0.0,
            ws_recv_ms_stamp=timings["ws_recv_ms"],
        )
    )


async def ws_loop():
    print(
        f"[WALLET-MODE] Следим за кошельками: {', '.join(WATCH_WALLETS)}\n"
        f"Если они покупают токен ≥ {MIN_WALLET_BUY_SOL} SOL — добавляем mint.\n"
        f"Если по токену из списка есть ПРОДАЖА, liq ≥ {MIN_LIQ_USD}$ и share ≥ {MIN_SELL_SHARE_PCT}% — входим.\n"
        f"Покупка делается строго из WSOL баланса (wrap не используем).\n"
        f"Jito tip: отдельной best-effort транзакцией перед swap (JITO_TIP_LAMPORTS={JITO_TIP_LAMPORTS})."
    )

    # прогрев ликвы
    if WATCHED_MINTS:

        async def warmup():
            for m in list(WATCHED_MINTS):
                try:
                    res = await asyncio.to_thread(get_liquidity_info, m)
                    if res is None:
                        continue
                    liq, sol_est = res
                    async with LIQ_LOCK:
                        LIQ_CACHE[m] = (liq, sol_est, time.time())
                    print(f"[DEX LIQ INIT] {m}: liq≈${liq:,.2f}, SOL_часть≈{sol_est:.4f} SOL")
                except Exception:
                    pass

        asyncio.create_task(warmup())

    while True:
        try:
            print(f"[WS] connecting → {PUMP_WSS_URL}")
            async with websockets.connect(PUMP_WSS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": WATCH_WALLETS}))
                print(f"[WS] subscribed to account(s): {', '.join(WATCH_WALLETS)}")

                async with WATCH_LOCK:
                    init_tokens = list(WATCHED_MINTS)
                if init_tokens:
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": init_tokens}))
                    print(f"[WS] subscribed to token(s): {', '.join(init_tokens)}")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(msg, dict) and "errors" in msg:
                        print("[WS ERROR MSG]", msg)
                        continue
                    await handle_trade_msg(msg, ws)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[WS ERROR] {repr(e)}; reconnect in 1.0s")
            await asyncio.sleep(1.0)


# ==========================
# MAIN
# ==========================

async def main():
    global MAIN_LOOP, WSOL_ACCOUNT, WSOL_BALANCE_LAMPORTS, WSOL_BALANCE_READY
    MAIN_LOOP = asyncio.get_running_loop()

    # 1) найдём РЕАЛЬНЫЙ WSOL token-account (может быть НЕ ATA)
    WSOL_ACCOUNT = await asyncio.to_thread(find_best_wsol_account, MY_PUBKEY)
    print(f"[WSOL] best token account = {WSOL_ACCOUNT} (ATA={WSOL_ATA})")

    # 2) выставим initial баланс сразу (чтобы не было 0 в первые миллисекунды)
    try:
        WSOL_BALANCE_LAMPORTS = int(get_wsol_balance_fast(MY_PUBKEY))
        WSOL_BALANCE_READY = True
        print(f"[WSOL] initial balance = {WSOL_BALANCE_LAMPORTS / 1e9:.9f} WSOL")
    except Exception as e:
        print(f"[WSOL] initial balance fetch failed: {e}")

    # 3) WSOL баланс в фоне (0 RPC в hot-path, если включено)
    if WSOL_WS_BALANCE:
        asyncio.create_task(wsol_balance_ws_listener())

    restore_autosell_from_disk()

    # ВАЖНО: refresher — основной источник 429 на Dexscreener.
    # Если хочешь включить обратно — раскомментируй и поставь в .env:
    # LIQ_REFRESH_SEC=60
    # LIQ_REFRESH_CONCURRENCY=1
    # asyncio.create_task(liquidity_refresher())

    await ws_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
