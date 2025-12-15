import os
import json
import time
import asyncio
import base64
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import requests
import websockets
from dotenv import load_dotenv

from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction as SoldersVTx
from solders.pubkey import Pubkey

from urllib.parse import urlparse, urlunparse

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
    """
    Преобразуем
      https://... → wss://...
      http://...  → ws://...

    Для Helius и похожих провайдеров обычно достаточно сменить схему.
    """
    p = urlparse(rpc_url)
    if p.scheme in ("http", "https"):
        ws_scheme = "wss" if p.scheme == "https" else "ws"
    else:
        ws_scheme = "wss"
    return urlunparse(
        (ws_scheme, p.netloc, p.path, p.params, p.query, p.fragment)
    )


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
    # На всякий случай убираем возможные префиксы вида WATCH_WALLET=...
    for p in parts:
        if p.startswith("WATCH_WALLET="):
            p = p.split("=", 1)[1].strip()
        if p:
            WATCH_WALLETS.append(p)

if not WATCH_WALLETS:
    raise RuntimeError("В .env не задан WATCH_WALLET (или пустой)")

MIN_WALLET_BUY_SOL = as_float(os.getenv("MIN_WALLET_BUY_SOL"), 0.01)
TRIGGER_SELL_SOL = as_float(os.getenv("TRIGGER_SELL_SOL"), 0.1)

BUY_SOL = as_float(os.getenv("BUY_SOL"), 0.01)
SLIPPAGE_BPS = as_int(os.getenv("SLIPPAGE_BPS"), 900)

# приоритетная комиссия для покупок
PRIORITY_FEE_LAMPORTS = as_int(os.getenv("PRIORITY_FEE_LAMPORTS"), 0)

# отдельная приора для продаж (autosell); если не задана — равна BUY
SELL_PRIORITY_FEE_LAMPORTS = as_int(
    os.getenv("SELL_PRIORITY_FEE_LAMPORTS"), PRIORITY_FEE_LAMPORTS,
)

# Автопродажа по умолчанию ВЫКЛЮЧЕНА (можно включить через .env)
AUTO_SELL = as_bool(os.getenv("AUTO_SELL"), False)
TP_PCT = as_float(os.getenv("AUTO_TP_PCT"), 5.0)   # 5%
SL_PCT = as_float(os.getenv("AUTO_SL_PCT"), 25.0)  # 25%

POLL_SECONDS = as_float(os.getenv("AUTO_SELL_POLL_INTERVAL"), 1.0)

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

# Диапазон падения ЦЕНЫ (в %), вызванный одной продажей, при котором входим
ONE_SELL_DROP_MIN_PCT = as_float(os.getenv("ONE_SELL_DROP_MIN_PCT"), 2.0)
ONE_SELL_DROP_MAX_PCT = as_float(os.getenv("ONE_SELL_DROP_MAX_PCT"), 70.0)

# Минимальная ликвидность, при которой бот вообще рассматривает вход
MIN_LIQ_USD = as_float(os.getenv("MIN_LIQ_USD"), 40_000.0)

# Минимальная доля SOL-части пула (в %) для одной продажи,
# чтобы считать её "достаточно большой".
MIN_SELL_SHARE_PCT = as_float(os.getenv("MIN_SELL_SHARE_PCT"), 1.0)

# Грубая оценка цены SOL в USD для расчёта доли SOL-части пула.
SOL_PRICE_USD = as_float(os.getenv("SOL_PRICE_USD"), 150.0)

WSOL_MINT = "So11111111111111111111111111111111111111112"

# SPL Token-2022 (из логов Jupiter/Pump)
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

print(f"[KEY CFG] RPC_URL={RPC_URL}")
print(f"[KEY CFG] RPC_WS_URL={RPC_WS_URL}")
print(
    f"[CFG] WATCH_WALLETS={','.join(WATCH_WALLETS)} "
    f" MIN_WALLET_BUY_SOL={MIN_WALLET_BUY_SOL}  TRIGGER_SELL_SOL={TRIGGER_SELL_SOL}"
)
print(
    f"[CFG] BUY_SOL={BUY_SOL}  SLIPPAGE_BPS={SLIPPAGE_BPS} "
    f"PRIO_BUY={PRIORITY_FEE_LAMPORTS}  PRIO_SELL={SELL_PRIORITY_FEE_LAMPORTS}"
)
print(f"[CFG] AUTO_SELL={AUTO_SELL} TP={TP_PCT}% SL={SL_PCT}% POLL={POLL_SECONDS}s")
print(f"[CFG] JUP_BASES={JUP_BASES} REQUIRE_PUMPFUN={REQUIRE_PUMPFUN} POOL_AMM_ID={POOL_AMM_ID}")
print(
    f"[CFG] ONE_SELL_DROP_MIN_PCT={ONE_SELL_DROP_MIN_PCT}  "
    f"ONE_SELL_DROP_MAX_PCT={ONE_SELL_DROP_MAX_PCT}  MIN_LIQ_USD={MIN_LIQ_USD}"
)
print(
    f"[CFG] MIN_SELL_SHARE_PCT={MIN_SELL_SHARE_PCT}  "
    f"SOL_PRICE_USD≈{SOL_PRICE_USD}"
)

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


def load_watched_mints() -> List[str]:
    """
    Читает watched_tokens.json. Формат:
    {
      "tokens": [
        "mint1",
        "mint2"
      ]
    }
    Если файл битый / пустой — просто возвращаем [] и работаем дальше.
    """
    if not TOKENS_FILE.exists():
        print("[TOKENS] watched_tokens.json не найден (начинаем с нуля).")
        return []
    try:
        data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        tokens = data.get("tokens") or []
        res = [str(m).strip() for m in tokens if str(m).strip()]
        return res
    except json.JSONDecodeError:
        print("[TOKENS] watched_tokens.json повреждён или пустой, игнорирую содержимое.")
        return []
    except Exception as e:
        print("[TOKENS] Ошибка при чтении watched_tokens.json:", repr(e))
        return []


def save_watched_mints(mints: List[str]) -> None:
    mset = sorted(set(mints))
    TOKENS_FILE.write_text(
        json.dumps({"tokens": mset}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[TOKENS] Сохранено {len(mset)} токенов в watched_tokens.json")


WATCHED_MINTS: List[str] = load_watched_mints()
if WATCHED_MINTS:
    print(f"[TOKENS] Загружено {len(WATCHED_MINTS)} токенов из watched_tokens.json")
else:
    print("[TOKENS] Список токенов пуст.")

SUBSCRIBED_TOKENS: set[str] = set(WATCHED_MINTS)

# ==========================
# PRICE CACHE + JUPITER COOLDOWN
# ==========================

TOKEN_PRICE_CACHE: Dict[str, float] = {}       # mint -> last price (WSOL per token)
TOKEN_DECIMALS_CACHE: Dict[str, int] = {}     # mint -> decimals
LIQ_CACHE: Dict[str, Tuple[float, float]] = {}  # mint -> (liq_usd, sol_in_pool_est)

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
# LIQUIDITY (Dexscreener)
# ==========================

def get_liquidity_info(mint: str) -> Tuple[float, float]:
    """
    Возвращает (liq_usd, sol_in_pool_est).

    liq_usd – максимальная ликвидность по Solana-парам из Dexscreener.
    sol_in_pool_est – грубая оценка количества SOL в пуле, исходя из:
      liq_usd ≈ 2 * sol_in_pool * SOL_PRICE_USD
      => sol_in_pool ≈ liq_usd / (2 * SOL_PRICE_USD)
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        r = DEX_SESSION.get(url, timeout=7)
        r.raise_for_status()
        j = r.json()
        pairs = j.get("pairs") or []
        if not pairs:
            return 0.0, 0.0

        best_liq = None
        best_pair = None
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
                best_pair = p

        if best_liq is None:
            p0 = pairs[0]
            liq0 = (p0.get("liquidity") or {}).get("usd")
            if liq0 is None:
                return 0.0, 0.0
            best_liq = float(liq0)
            best_pair = p0

        # Грубая оценка SOL в пуле: считаем, что пул примерно 50/50.
        if SOL_PRICE_USD > 0:
            sol_in_pool_est = best_liq / (2.0 * SOL_PRICE_USD)
        else:
            sol_in_pool_est = 0.0

        return float(best_liq), float(sol_in_pool_est)
    except Exception as e:
        print(f"[DEX LIQ] Ошибка Dexscreener для {mint}: {e}")
        return 0.0, 0.0


def get_liquidity_info_cached(mint: str) -> Tuple[float, float]:
    """
    Кэшируем ликвидность по mint. Dexscreener дергается максимум 1 раз на токен за запуск.
    """
    if mint in LIQ_CACHE:
        return LIQ_CACHE[mint]
    liq, sol_est = get_liquidity_info(mint)
    LIQ_CACHE[mint] = (liq, sol_est)
    return liq, sol_est


def get_liquidity_usd(mint: str) -> float:
    liq, _ = get_liquidity_info_cached(mint)
    return liq


# ==========================
# WATCH LIST
# ==========================

def add_watched_mint(mint: str):
    if mint not in WATCHED_MINTS:
        WATCHED_MINTS.append(mint)
        save_watched_mints(WATCHED_MINTS)
        print(f"[TOKENS] Добавлен mint в watch-лист: {mint}")


# ==========================
# Jupiter
# ==========================

def _debug_print_route(obj: dict, tag: str):
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
        "Маршрут через Pump.fun не найден"
        + (f" (требовался пул {pool_amm_id})" if pool_amm_id else "")
    )


def jup_quote_pump_only(
    input_mint: str,
    output_mint: str,
    amount_smallest: int,
    require_pump: Optional[bool] = None,
    pool_amm_id: Optional[str] = None,
) -> dict:
    """
    "Тяжёлый", но надёжный квотер — используется для:
    - get_token_price_wsol (цена)
    - autosell (SELL)

    Для входа мы используем более быстрый jup_quote_for_entry.
    """
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

    fast_variants = [
        {**base},
        {**base, "onlyDirectRoutes": "true"},
        {**base, "dexes": "pump"},
        {**base, "dexes": ["pump", "pump.fun"]},
    ]
    fallback_variants = [
        {**base},
        {**base, "excludeDexes": "meteora"},
        {**base, "excludeDexes": ["meteora"]},
    ]

    last_err = None
    max_rounds = 2

    # FAST
    for round_idx in range(max_rounds):
        for var_idx, params in enumerate(fast_variants, 1):
            try:
                r = http_get_with_fallback("/swap/v1/quote", params=params, timeout=10)
                r.raise_for_status()
                obj = r.json()
                _debug_print_route(obj, f"FAST r{round_idx+1}/v{var_idx}")
                return _filter_routes_pumpfun_strict(obj, require_pump, pool_amm_id)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    raise RuntimeError("Jupiter 429 Too Many Requests") from e
                last_err = e
                time.sleep(0.15 + 0.1 * round_idx)
            except Exception as e:
                last_err = e
                time.sleep(0.15 + 0.1 * round_idx)

    # FALLBACK
    for round_idx in range(max_rounds):
        for var_idx, params in enumerate(fallback_variants, 1):
            try:
                r = http_get_with_fallback("/swap/v1/quote", params=params, timeout=10)
                r.raise_for_status()
                obj = r.json()
                _debug_print_route(obj, f"FALLBACK r{round_idx+1}/v{var_idx}")
                return _filter_routes_pumpfun_strict(obj, require_pump, pool_amm_id)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    raise RuntimeError("Jupiter 429 Too Many Requests") from e
                last_err = e
                time.sleep(0.2 + 0.1 * round_idx)
            except Exception as e:
                last_err = e
                time.sleep(0.2 + 0.1 * round_idx)

    if isinstance(last_err, RuntimeError):
        raise last_err

    raise RuntimeError("Маршрут не найден через Jupiter после нескольких попыток.")


def jup_quote_for_entry(
    input_mint: str,
    output_mint: str,
    amount_smallest: int,
    require_pump: Optional[bool] = None,
    pool_amm_id: Optional[str] = None,
) -> dict:
    """
    УПРОЩЁННЫЙ, БЫСТРЫЙ квотер ДЛЯ ВХОДА.

    — минимум параметров;
    — не пытаемся жёстко втащить Pump.fun (для скорости);
    — 1–2 быстрые попытки к /swap/v1/quote;
    — если маршрут не найден → просто ENTRY_QUOTE_FAILED, сигнал скипаем.
    """
    if require_pump is None:
        # Для входа по сигналу нам теперь важнее СКОРОСТЬ, а не жёсткий Pump.fun.
        require_pump = False
    if pool_amm_id is None:
        pool_amm_id = None

    base = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_smallest),
        "slippageBps": SLIPPAGE_BPS,
    }

    # Очень простой набор вариантов: без лишних фильтров.
    variants = [
        {**base},  # обычный маршрут
        {**base, "onlyDirectRoutes": "true"},  # прямой маршрут, если есть
    ]

    last_err: Optional[Exception] = None

    for idx, params in enumerate(variants, 1):
        try:
            r = http_get_with_fallback("/swap/v1/quote", params=params, timeout=6)
            r.raise_for_status()
            obj = r.json()
            _debug_print_route(obj, f"ENTRY v{idx}")
            # Если вдруг всё-таки хочешь иногда требовать Pump, фильтр учитывает require_pump
            return _filter_routes_pumpfun_strict(obj, require_pump, pool_amm_id)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                # 429 — сразу выбрасываем, чтобы выше можно было включить кулдаун
                raise RuntimeError("Jupiter 429 Too Many Requests") from e
            last_err = e
        except Exception as e:
            last_err = e

    if isinstance(last_err, RuntimeError):
        raise last_err

    raise RuntimeError("ENTRY_QUOTE_FAILED")


def jup_swap(
    route_obj: dict,
    user_pubkey: str,
    priority_fee_lamports: Optional[int] = None,
) -> str:
    """
    priority_fee_lamports:
      - None -> использовать глобальный PRIORITY_FEE_LAMPORTS (для покупок);
      - число -> использовать его (например SELL_PRIORITY_FEE_LAMPORTS для продаж).
    """
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
    r.raise_for_status()
    data = r.json()
    tx_b64 = data.get("swapTransaction")
    if not tx_b64:
        raise RuntimeError(f"Jupiter swap не вернул swapTransaction: {data}")
    return tx_b64


# ==========================
# BALANCES / ATA
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
    res = rpc_call(
        "getTokenAccountsByOwner",
        [
            owner,
            {"mint": mint},
            {
                "encoding": "jsonParsed",
                "commitment": "processed",
            },
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


def get_ata_address(mint: str, owner: str) -> str:
    mint_pk = Pubkey.from_string(mint)
    owner_pk = Pubkey.from_string(owner)
    ata_pk, _ = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(TOKEN_PROGRAM_ID), bytes(mint_pk)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return str(ata_pk)


async def wait_for_token_via_ws(mint: str, owner: str, timeout: float = 30.0) -> tuple[int, int]:
    ata = get_ata_address(mint, owner)
    print(f"[WS-ATA] ATA для {mint} и {owner}: {ata}")

    bal_raw, dec = get_token_balance_raw(mint, owner)
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
                "params": [
                    ata,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "processed",
                    },
                ],
            }
            await ws.send(json.dumps(sub))
            start_ts = time.time()
            while True:
                if timeout and (time.time() - start_ts) > timeout:
                    print(f"[WS-ATA] Таймаут ожидания уведомления по {ata}, фолбэк на polling.")
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

                print(f"[WS-ATA] accountNotification по {ata}: amount={amount}, decimals={decimals}")
                if amount > 0:
                    print(f"[WS-ATA] Токен {mint} появился на кошельке, raw={amount}")
                    return amount, decimals
    except Exception as e:
        print(f"[WS-ATA] Ошибка WS-подписки по ATA {ata}: {e}")

    print("[WS-ATA] Перехожу к HTTP polling’у баланса токена…")
    while True:
        bal_raw, dec = get_token_balance_raw(mint, owner)
        if bal_raw > 0:
            print(f"[WS-ATA] Токен обнаружен при polling: raw={bal_raw}")
            return bal_raw, dec
        await asyncio.sleep(POLL_SECONDS)


# ==========================
# TOKEN PRICE via Jupiter (с кулдауном)
# ==========================

def get_token_price_wsol(mint: str) -> float:
    """
    Получает цену токена в WSOL за 1 токен.
    Сначала пробуем маршрут через Pump.fun, если его нет — любой маршрут.
    Если Jupiter даёт 429 — включаем глобальный кулдаун.
    """
    if jup_in_cooldown():
        raise RuntimeError("Jupiter cooldown active")

    dec = get_mint_decimals_cached(mint)
    amount_smallest = 10 ** dec  # 1 токен

    try:
        quote = jup_quote_pump_only(
            input_mint=mint,
            output_mint=WSOL_MINT,
            amount_smallest=amount_smallest,
            require_pump=True,
            pool_amm_id=None,
        )
    except RuntimeError as e:
        msg = str(e)
        if "Jupiter 429 Too Many Requests" in msg:
            print(f"[PRICE] Jupiter 429 при попытке взять цену {mint}, включаю кулдаун 10с")
            jup_set_cooldown(10.0)
            raise
        if "Маршрут через Pump.fun не найден" not in msg:
            raise

        try:
            quote = jup_quote_pump_only(
                input_mint=mint,
                output_mint=WSOL_MINT,
                amount_smallest=amount_smallest,
                require_pump=False,
                pool_amm_id=None,
            )
        except RuntimeError as e2:
            msg2 = str(e2)
            if "Jupiter 429 Too Many Requests" in msg2:
                print(f"[PRICE] Jupiter 429 даже на fallback для {mint}, кулдаун 10с")
                jup_set_cooldown(10.0)
            raise

    out_lamports = int(quote.get("outAmount", "0") or "0")
    if out_lamports <= 0:
        raise RuntimeError(f"Jupiter вернул outAmount=0 для mint={mint}")

    price_wsol = out_lamports / 1_000_000_000  # WSOL за 1 токен
    return price_wsol


# ==========================
# SEND TX
# ==========================

def send_signed_sync(base64_tx: str) -> str:
    raw = base64.b64decode(base64_tx)
    tx = SoldersVTx.from_bytes(raw)
    tx_signed = SoldersVTx(tx.message, [KP])

    try:
        wire = bytes(tx_signed.serialize())
    except Exception:
        wire = bytes(tx_signed)

    b64_signed = base64.b64encode(wire).decode()

    send_opts = {
        "encoding": "base64",
        "skipPreflight": SKIP_PREFLIGHT,
        "preflightCommitment": "processed" if FAST_CONFIRM else "confirmed",
    }

    sig_str = rpc_call("sendTransaction", [b64_signed, send_opts])
    print("⛓ sent:", sig_str)

    deadline = time.time() + 40
    last_status = None

    while time.time() < deadline:
        statuses = rpc_call(
            "getSignatureStatuses",
            [[sig_str], {"searchTransactionHistory": True}],
        )
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
                else:
                    print("❌ on-chain tx error:", err, "sig:", sig_str)
                    raise RuntimeError(f"Transaction {sig_str} failed on-chain: {err}")
        else:
            if conf in ("confirmed", "finalized"):
                if err is None:
                    print(f"✅ success ({conf}) {sig_str}")
                    return sig_str
                else:
                    print("❌ on-chain tx error:", err, "sig:", sig_str)
                    raise RuntimeError(f"Transaction {sig_str} failed on-chain: {err}")

        time.sleep(0.4)

    raise RuntimeError(f"Timeout ожидания подтверждения. Последний статус: {last_status}")


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


def save_positions_from_active() -> None:
    """
    Сохраняем все активные позиции в positions.json.
    При перезапуске по ним будут подняты autosell_worker’ы.
    """
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
        POSITIONS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[POS] Сохранено {len(payload['positions'])} активных позиций в {POSITIONS_FILE.name}"
        )
    except Exception as e:
        print(f"[POS] Ошибка сохранения позиций: {e}")


def restore_autosell_from_disk() -> None:
    """
    При старте бота читаем positions.json и поднимаем autosell_worker
    для всех активных позиций, если включён AUTO_SELL.
    """
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
                entry_price = float(
                    p.get("entry_price_wsol_per_token")
                    or p.get("entry_price")
                    or 0.0
                )
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
            print(
                f"[POS] Восстановлена позиция по {mint}, "
                f"entry≈{entry_price:.10f} WSOL/токен"
            )
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
    print(f"[AUTOSELL] Обнаружен баланс токена (через WS/HTTP): raw={bal_raw}, decimals={dec}")

    target_up = 1.0 + TP_PCT / 100.0
    target_down = 1.0 - SL_PCT / 100.0

    while entry.active:
        bal_raw, dec = get_token_balance_raw(mint, MY_PUBKEY)
        if bal_raw <= 0:
            print(f"[AUTOSELL] Баланс {mint} = 0, выхожу из цикла.")
            entry.active = False
            # Чистим запись о позиции и сохраняем файл
            if mint in ACTIVE_ENTRIES:
                del ACTIVE_ENTRIES[mint]
            save_positions_from_active()
            break

        amount_tokens = bal_raw / (10 ** dec)

        try:
            quote = jup_quote_pump_only(
                input_mint=mint,
                output_mint=WSOL_MINT,
                amount_smallest=bal_raw,
                require_pump=True,
                pool_amm_id=entry.amm_key,
            )
        except Exception as e:
            msg = str(e)
            if "429 Too Many Requests" in msg:
                print("[AUTOSELL] Jupiter rate-limit (429), пауза 8 секунд...")
                await asyncio.sleep(8.0)
            else:
                print(f"[AUTOSELL] Ошибка jup_quote_pump_only: {e}")
                await asyncio.sleep(POLL_SECONDS)
            continue

        out_lamports = int(quote.get("outAmount", "0") or "0")
        if out_lamports <= 0:
            print("[AUTOSELL] Jupiter вернул outAmount=0, жду...")
            await asyncio.sleep(POLL_SECONDS)
            continue

        est_wsol = out_lamports / 1_000_000_000
        price = est_wsol / amount_tokens
        ratio = price / entry.entry_price_wsol_per_token

        print(f"[AUTOSELL] price={price:.10f} WSOL/токен ratio={ratio:.4f} (1.0 = вход)")

        if ratio >= target_up:
            print(f"[AUTOSELL] 🎯 TAKE PROFIT ({ratio:.2f}x) — продаю всё в WSOL.")
        elif ratio <= target_down:
            print(f"[AUTOSELL] 🛑 STOP LOSS ({ratio:.2f}x) — продаю всё в WSOL.")
        else:
            await asyncio.sleep(POLL_SECONDS)
            continue

        # SELL
        try:
            quote_sell = jup_quote_pump_only(
                input_mint=mint,
                output_mint=WSOL_MINT,
                amount_smallest=bal_raw,
                require_pump=True,
                pool_amm_id=entry.amm_key,
            )
        except Exception as e:
            msg = str(e)
            if "429 Too Many Requests" in msg:
                print("[AUTOSELL] Jupiter rate-limit (429) при SELL, пауза 8 секунд...")
                await asyncio.sleep(8.0)
            else:
                print(f"[AUTOSELL] Ошибка при запросе quote для SELL: {e}")
                await asyncio.sleep(POLL_SECONDS)
            continue

        route = quote_sell.get("routePlan") or []
        if route:
            info = route[0].get("swapInfo") or {}
            label = info.get("label")
            ammKey = info.get("ammKey")
            print(f"✅ SELL маршрут: {label} ({ammKey})")

        try:
            # Продажа: используем отдельную повышенную приоритетную комиссию
            b64_tx = jup_swap(
                quote_sell,
                MY_PUBKEY,
                priority_fee_lamports=SELL_PRIORITY_FEE_LAMPORTS,
            )
            sig = send_signed_sync(b64_tx)
            print(f"[AUTOSELL] Продажа завершена, сигнатура: {sig}")
            entry.active = False
            if mint in ACTIVE_ENTRIES:
                del ACTIVE_ENTRIES[mint]
            save_positions_from_active()
        except Exception as e:
            print(f"[AUTOSELL] Ошибка при отправке SELL-транзакции: {e}")
            await asyncio.sleep(POLL_SECONDS)

    print(f"[AUTOSELL] Завершён для {mint}")


async def enter_token_on_signal(
    mint: str,
    sol_amt: float,
    sell_share_pct: float,
    drop_pct: float,
):
    lamports_in = int(BUY_SOL * 1_000_000_000)
    print(
        f"🔥 [TOKEN {mint}] Сигнал входа: продажа {sol_amt:.4f} SOL "
        f"({sell_share_pct:.2f}% пула), падение цены {drop_pct:.2f}% (по старому кэшу, можно игнорить). "
        f"Покупаю на {BUY_SOL} SOL через Jupiter…"
    )

    # ============================
    # ОДИН быстрый квотер через Jupiter (без тяжёлых фоллбеков)
    # ============================
    quote: Optional[dict] = None

    try:
        # Для входа нам важна скорость → require_pump=False (любой нормальный маршрут).
        quote = jup_quote_for_entry(
            input_mint=WSOL_MINT,
            output_mint=mint,
            amount_smallest=lamports_in,
            require_pump=False,
            pool_amm_id=None,
        )
    except Exception as e:
        msg = str(e)
        if "Jupiter 429 Too Many Requests" in msg:
            print(f"[ENTRY/{mint}] Jupiter 429 на fast-quote, скипаю этот сигнал.")
            return
        print(f"[ENTRY/{mint}] Ошибка fast-quote (без тяжёлых фоллбеков): {e}")
        return

    if quote is None:
        print(f"[ENTRY/{mint}] Не удалось получить маршрут через Jupiter. Скип.")
        return

    out_raw = int(quote.get("outAmount", "0") or "0")
    if out_raw <= 0:
        print(f"[ENTRY/{mint}] outAmount=0, отменяю.")
        return

    # === Правильный расчёт входной цены: SOL за 1 целый токен ===
    dec = get_mint_decimals_cached(mint)
    amount_tokens = out_raw / (10 ** dec)
    if amount_tokens <= 0:
        print(f"[ENTRY/{mint}] amount_tokens<=0, отменяю.")
        return

    sol_in = lamports_in / 1_000_000_000  # сколько SOL потратили
    entry_price_wsol_per_token = sol_in / amount_tokens  # SOL за 1 токен

    amm_key = None
    label = None
    rp = quote.get("routePlan") or []
    if rp:
        info = rp[0].get("swapInfo") or {}
        amm_key = (info.get("ammKey") or "").strip()
        label = info.get("label")

    print(
        f"[ENTRY/TOKEN {mint}] Входная цена ≈ {entry_price_wsol_per_token:.10f} WSOL/токен "
        f"(~{amount_tokens:.4f} токенов, raw={out_raw})"
    )
    if label or amm_key:
        print(f"✅ Маршрут через {label}, пул: {amm_key}")

    try:
        # Покупка: используем стандартную приору для BUY (PRIORITY_FEE_LAMPORTS)
        b64_tx = jup_swap(quote, MY_PUBKEY)
        sig = send_signed_sync(b64_tx)
        print(f"[ENTRY/TOKEN {mint}] Покупка отправлена, сигнатура: {sig}")
    except Exception as e:
        print(f"[ENTRY/TOKEN {mint}] Ошибка отправки swap-транзакции: {e}")
        return

    if AUTO_SELL:
        state = EntryState(
            mint=mint,
            entry_price_wsol_per_token=entry_price_wsol_per_token,
            amm_key=amm_key,
            decimals=dec,
        )
        ACTIVE_ENTRIES[mint] = state
        save_positions_from_active()
        asyncio.create_task(autosell_worker(state))


# ==========================
# PUMPPORTAL WS
# ==========================

PUMP_WSS_URL = f"wss://pumpportal.fun/api/data?api-key={PUMP_API_KEY}"


def _get_side_flags(msg: dict) -> Tuple[bool, bool]:
    """
    Возвращает (is_buy, is_sell) из сообщения pumpportal.

    Приоритет:
      1) булевое поле is_buy / isBuy (доверяем ему как основному источнику)
      2) строковое поле side / tradeType / txType ('buy' / 'sell')
      3) если непонятно — считаем, что ни buy, ни sell (False, False)
    """
    is_buy_raw = msg.get("is_buy")
    if is_buy_raw is None:
        is_buy_raw = msg.get("isBuy")

    # Если явно булевое значение — это самый надёжный источник.
    if isinstance(is_buy_raw, bool):
        return bool(is_buy_raw), (not bool(is_buy_raw))

    # Фолбэк на строковый side / tradeType / txType
    side_raw = (
        str(
            msg.get("side")
            or msg.get("tradeType")
            or msg.get("txType")
            or ""
        )
    ).lower()

    if side_raw == "buy":
        return True, False
    if side_raw == "sell":
        return False, True

    # Ничего внятного — лучше вообще не считать это сигналом
    print(
        f"[DEBUG SIDE] Не удалось определить side: is_buy={msg.get('is_buy')}, "
        f"isBuy={msg.get('isBuy')}, side={msg.get('side')}, "
        f"tradeType={msg.get('tradeType')}, txType={msg.get('txType')}"
    )
    mint_dbg = (
        msg.get("mint")
        or msg.get("token")
        or msg.get("tokenMint")
        or msg.get("tokenAddress")
    )
    print(
        f"[DEBUG RAW] msg for mint {mint_dbg}: "
        f"keys={list(msg.keys())}, is_buy={msg.get('is_buy')}, isBuy={msg.get('isBuy')}, "
        f"side={msg.get('side')}, tradeType={msg.get('tradeType')}, txType={msg.get('txType')}"
    )
    return False, False


async def handle_trade_msg(msg: dict, ws):
    """
    Обработка одного сообщения от pumpportal.

    Логика:
      1) следим за покупками отслеживаемых кошельков → добавляем mint в watch-лист;
      2) по токенам из watch-листа:
         - рассматриваем ТОЛЬКО ПРОДАЖИ (sell), определённые по полям is_buy/isBuy/side/txType;
         - токен должен иметь ликвидность ≥ MIN_LIQ_USD,
         - одна продажа должна быть не меньше MIN_SELL_SHARE_PCT % от SOL-части пула,
         - без проверки drop_pct по цене (для экономии квотов Jupiter),
         → входим в токен.
    """
    # control-сообщения типа {"message":"Successfully subscribed to keys."}
    if "mint" not in msg and "token" not in msg and "tokenMint" not in msg and "tokenAddress" not in msg:
        return

    mint = (
        msg.get("mint")
        or msg.get("token")
        or msg.get("tokenMint")
        or msg.get("tokenAddress")
    )
    if not mint:
        return

    # Ищем адрес трейдера во всех возможных полях pumpportal
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

    # Фолбэк на старую схему, если вдруг ничего не нашли
    if trader is None:
        trader = (
            msg.get("wallet")
            or msg.get("account")
            or msg.get("owner")
            or msg.get("trader")
            or msg.get("user")
        )

    seller = trader or "UNKNOWN"

    # 🧠 НОВОЕ: аккуратно определяем buy/sell
    is_buy, is_sell = _get_side_flags(msg)
    if not is_buy and not is_sell:
        # Непонятный тип операции — пропускаем
        return

    sol_amt = 0.0
    for k in ("sol_amount", "solAmount", "native", "nativeAmount", "solAmount"):
        if k in msg and msg[k] is not None:
            try:
                sol_amt = float(msg[k])
                break
            except Exception:
                continue

    # 1) Покупка отслеживаемым кошельком -> добавить mint + подписаться на trades по этому mint
    if trader in WATCH_WALLETS and is_buy and sol_amt >= MIN_WALLET_BUY_SOL:
        print(
            f"[WALLET-BUY] {trader} купил токен {mint} на {sol_amt:.4f} SOL "
            f"(порог {MIN_WALLET_BUY_SOL})"
        )
        add_watched_mint(mint)
        if mint not in SUBSCRIBED_TOKENS:
            sub = {
                "method": "subscribeTokenTrade",
                "keys": [mint],
            }
            await ws.send(json.dumps(sub))
            SUBSCRIBED_TOKENS.add(mint)
            print(f"[WS] subscribed to token: {mint}")

    # 2) Нас интересуют сигналы только по токенам из WATCHED_MINTS
    if mint not in WATCHED_MINTS:
        return

    # 3) Нас интересуют ТОЛЬКО ПРОДАЖИ
    if not is_sell:
        # Это покупка — для стратегии входа по продаже игнорируем
        return

    if sol_amt <= 0:
        return

    # 4) Проверяем ликвидность — через кэш
    cur_liq, sol_in_pool_est = get_liquidity_info_cached(mint)
    if cur_liq < MIN_LIQ_USD:
        print(
            f"[TOKEN {mint}] Ликвидность слишком низкая (${cur_liq:.2f} < ${MIN_LIQ_USD:.2f}), "
            f"не рассматриваю этот токен для входа."
        )
        return

    # 4.1 Оценка доли SOL-части пула, которая вышла за одну продажу
    sell_share_pct = 0.0
    if sol_in_pool_est > 0:
        sell_share_pct = 100.0 * sol_amt / sol_in_pool_est

    if sol_in_pool_est <= 0:
        print(
            f"[TOKEN {mint}] Не удалось оценить SOL в пуле (liq=${cur_liq:.2f}), "
            f"но ликвидность ок, смотрю только на размер продажи в SOL."
        )
    else:
        if sell_share_pct < MIN_SELL_SHARE_PCT:
            print(
                f"[TOKEN {mint}] Продажа {sol_amt:.4f} SOL слишком мала: "
                f"{sell_share_pct:.2f}% пула (< {MIN_SELL_SHARE_PCT}%). Скип."
            )
            return

    # 5) Больше НЕ проверяем падение цены через Jupiter (экономим квоты).
    #    Просто используем старый кэш для красивого лога.
    prev_price = TOKEN_PRICE_CACHE.get(mint)
    cur_price = prev_price if prev_price is not None else 0.0
    drop_pct = 0.0

    print(
        f"[TOKEN {mint}] ПРОДАЖА на {sol_amt:.4f} SOL от {seller}. "
        f"liq=${cur_liq:.0f} share≈{sell_share_pct:.2f}%"
    )

    print(
        f"🔥 [TOKEN {mint}] ОДНА продажа на {sol_amt:.4f} SOL вызвала падение цены {drop_pct:.2f}% "
        f"и даёт ~{sell_share_pct:.2f}% SOL-части пула. Вхожу в токен по стратегии."
    )

    await enter_token_on_signal(
        mint,
        sol_amt=sol_amt,
        sell_share_pct=sell_share_pct,
        drop_pct=drop_pct,
    )


async def ws_loop():
    print(
        f"[WALLET-MODE] Следим за кошельками: {', '.join(WATCH_WALLETS)}\n"
        f"Если они покупают токен ≥ {MIN_WALLET_BUY_SOL} SOL — добавляем mint в список.\n"
        f"Если по токену из списка есть ПРОДАЖА (любая > 0 SOL), ликва ≥ {MIN_LIQ_USD}$, "
        f"эта продажа даёт ≥ {MIN_SELL_SHARE_PCT}% SOL-части пула (по оценке) — входим (без проверки падения цены)."
    )

    # Прогрев ликвидности по токенам из файла (не обязательно, но красиво)
    if WATCHED_MINTS:
        print(f"[DEX LIQ INIT] Прогрев ликвидности для {len(WATCHED_MINTS)} токенов...")
        for m in WATCHED_MINTS:
            liq, sol_est = get_liquidity_info_cached(m)
            print(f"[DEX LIQ INIT] {m}: liq≈${liq:,.2f}, SOL_часть≈{sol_est:.4f} SOL")

    while True:
        try:
            print(f"[WS] connecting → {PUMP_WSS_URL}")
            async with websockets.connect(PUMP_WSS_URL, ping_interval=20, ping_timeout=20) as ws:
                sub_acc = {
                    "method": "subscribeAccountTrade",
                    "keys": WATCH_WALLETS,
                }
                await ws.send(json.dumps(sub_acc))
                print(f"[WS] subscribed to account(s): {', '.join(WATCH_WALLETS)}")

                if WATCHED_MINTS:
                    sub_tok = {
                        "method": "subscribeTokenTrade",
                        "keys": WATCHED_MINTS,
                    }
                    await ws.send(json.dumps(sub_tok))
                    print(f"[WS] subscribed to token(s): {', '.join(WATCHED_MINTS)}")

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
    # Восстанавливаем позиции из файла и поднимаем autosell
    restore_autosell_from_disk()
    await ws_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
