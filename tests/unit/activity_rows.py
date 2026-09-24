"""Blockscout advanced-filters rows, reduced from live reads of real wallets.

Each row keeps the fields the classifier reads and the shape the explorer
gives them: hashes and timestamps, `from`/`to` objects with `is_contract`,
`value` and `fee` in wei as strings, `total` with its decimals, the `token`
object, and `internal_transaction_index` null on top-level rows.

The week of 0xDD8a… on Base holds the finding that chose the endpoint: an EIP-7702
account whose Aave supply and LP withdrawal were submitted by a relayer, so
neither is in `/addresses/{a}/transactions`. The 10 Aug day of 0xB26B… holds
the poisoning: homoglyph "USDC" tokens sent from the wallet to lookalikes of a
real counterparty, and a dust deposit from one of them.
"""

from __future__ import annotations

from typing import Any

WALLET = "0xdd8ac30cb0a219af4963eab5e30ed065b5e63d68"
OTHER_WALLET = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"

ZERO = "0x0000000000000000000000000000000000000000"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
AWETH = "0xd4a0e0b9149bcee3c920d2e00b5de09138fd8bb7"
DEBT_USDC = "0x59dca05b6c26dbd64b5381374aaac5cd05644c28"
AUSDC = "0x4e65fe4dba92790696d040ac24aa414708f5c0ab"
V3_MANAGER = "0x03a520b32c04bf3beef7beb72e919cf822ed34f1"
GATEWAY = "0xa0d9c1e9e48ca30c8d8c3b5d69ff5dc1f6dffc24"
DELEGATION_MANAGER = "0xdb9b1e94b5b69df7e401ddbede43491141047db3"
RELAYER_FEES = "0xe3478b0bb1a5084567c319096437924948be1964"
ROUTER = "0x9dda6ef3d919c9bc8885d5560999a3640431e8e6"
SWAP_POOL = "0x0a2854fbbd9b3ef66f17d47284e7f899b9509330"
AIRDROPPER = "0x397d17bfcb8145e6bfbb4d1168c1b65de52c3445"
AI_TOKEN = "0x486f662020286e17e7469df4e5f2cf2415f36662"

REAL_PAYEE = "0xc4b3f1f29024742ca57b1667fb99fa0399eda52d"
LOOKALIKE_1 = "0xc4b371e86c33d9dc8912f29f60cbe409ed4da52d"
LOOKALIKE_2 = "0xc4b3a60d1f0c0eb91cb07a9d06a8e88495ada52d"
FAKE_USDC_1 = "0xecb66345cc206b252fa8e54cfdd78c60f655ce51"
FAKE_USDC_2 = "0x6c9458b7e1c1742c68d2662ea6a41ac5de43d28c"
ZERO_VALUE_LOOKALIKE = "0x09dbc2aa4c1e5d0f0e5d5f7c3b2a1908f7e6ab12"
ZERO_VALUE_REAL = "0x09dbc0bb2f3e4d5c6b7a8998a7b6c5d4e3f2ab12"
"""The zero-value `transferFrom` pair, shaped like the one seen live (the
lookalike shares the real payee's first and last four characters)."""


def party(address: str, *, contract: bool = True, name: str | None = None) -> dict[str, Any]:
    return {
        "hash": address,
        "is_contract": contract,
        "is_scam": False,
        "reputation": "ok",
        "name": name,
    }


def token(address: str, symbol: str, decimals: int) -> dict[str, Any]:
    # Blockscout's reputation is "ok" on every poisoned token seen live, so
    # the fixtures say so too: nothing may rely on it.
    return {
        "address_hash": address,
        "symbol": symbol,
        "decimals": str(decimals),
        "reputation": "ok",
        "type": "ERC-20",
    }


def native(
    tx: str,
    sender: str,
    receiver: str,
    value: int,
    *,
    at: str,
    method: str | None = None,
    fee: int = 0,
    internal: int | None = None,
    receiver_contract: bool = True,
    sender_contract: bool = True,
) -> dict[str, Any]:
    return {
        "hash": tx,
        "timestamp": at,
        "type": "contract_interaction" if method else "coin_transfer",
        "method": method,
        "from": party(sender, contract=sender_contract),
        "to": party(receiver, contract=receiver_contract),
        "value": str(value),
        "total": None,
        "token": None,
        "fee": str(fee),
        "internal_transaction_index": internal,
    }


def erc20(
    tx: str,
    sender: str,
    receiver: str,
    raw: int,
    coin: dict[str, Any],
    *,
    at: str,
    method: str = "0xcef6d209",
    sender_contract: bool = True,
    receiver_contract: bool = True,
) -> dict[str, Any]:
    return {
        "hash": tx,
        "timestamp": at,
        "type": "ERC-20",
        "method": method,
        "from": party(sender, contract=sender_contract),
        "to": party(receiver, contract=receiver_contract),
        "value": None,
        "total": {"value": str(raw), "decimals": coin["decimals"]},
        "token": coin,
        "fee": "0",
        "internal_transaction_index": None,
    }


def nft(
    tx: str, sender: str, receiver: str, manager: str, token_id: str, *, at: str
) -> dict[str, Any]:
    return {
        "hash": tx,
        "timestamp": at,
        "type": "ERC-721",
        "method": "0xac9650d8",
        "from": party(sender),
        "to": party(receiver),
        "value": None,
        "total": {"token_id": token_id, "decimals": None, "value": None},
        "token": {"address_hash": manager, "symbol": "UNI-V3-POS", "type": "ERC-721"},
        "fee": "0",
        "internal_transaction_index": None,
    }


USDC_TOKEN = token(USDC, "USDC", 6)
AWETH_TOKEN = token(AWETH, "AWETH", 18)
DEBT_USDC_TOKEN = token(DEBT_USDC, "variableDebtBasUSDC", 6)

SWAP_TX = "0x091ae620" + "0" * 56
LP_RELAYED_TX = "0x92367040" + "0" * 56
SUPPLY_RELAYED_TX = "0xf9f8223e" + "0" * 56
LP_SENT_TX = "0xe7dde074" + "0" * 56
AIRDROP_TX = "0xef323813" + "0" * 56


def base_week() -> list[dict[str, Any]]:
    """0xDD8a… on Base, 16-23 Sep 2026: newest first, as the explorer lists them."""
    return [
        # An airdropped "AI" token nobody listed: hidden, counted.
        erc20(
            AIRDROP_TX, AIRDROPPER, WALLET, 10**18, token(AI_TOKEN, "AI", 18),
            at="2026-09-20T00:49:11.000000Z", method="0x67243482", sender_contract=False,
        ),
        # ETH -> USDC through the MetaMask router, an unverified selector: the
        # flows say swap, the method name cannot.
        native(
            SWAP_TX, WALLET, ROUTER, 3996970724818315,
            at="2026-09-18T16:22:05.000000Z", method="0x5f575529", fee=1196567534093,
        ),
        erc20(SWAP_TX, SWAP_POOL, WALLET, 10280442, USDC_TOKEN, at="2026-09-18T16:22:05.000000Z"),
        # A relayed LP withdrawal: the wallet sent nothing, a relayer did, and
        # took a USDC cut to a plain address.
        erc20(
            LP_RELAYED_TX, WALLET, RELAYER_FEES, 9589, USDC_TOKEN,
            at="2026-09-18T16:21:41.000000Z", receiver_contract=False,
        ),
        erc20(LP_RELAYED_TX, V3_MANAGER, WALLET, 10502686, USDC_TOKEN,
              at="2026-09-18T16:21:41.000000Z"),
        # A relayed Aave supply of ETH through the gateway: the aWETH minted
        # (0.0310) includes interest; the ETH that left (0.0222) is the supply.
        native(
            SUPPLY_RELAYED_TX, WALLET, GATEWAY, 22208970712454581,
            at="2026-09-18T16:20:33.000000Z", method="0x474cf53d", internal=8, fee=835326139221,
        ),
        native(
            SUPPLY_RELAYED_TX, DELEGATION_MANAGER, WALLET, 0,
            at="2026-09-18T16:20:33.000000Z", method="0xd691c964", internal=7, fee=1071822178637,
        ),
        erc20(
            SUPPLY_RELAYED_TX, WALLET, RELAYER_FEES, 7447, USDC_TOKEN,
            at="2026-09-18T16:20:33.000000Z", receiver_contract=False,
        ),
        erc20(
            SUPPLY_RELAYED_TX, ZERO, WALLET, 31002276126759381, AWETH_TOKEN,
            at="2026-09-18T16:20:33.000000Z",
        ),
        # An LP withdrawal the wallet sent itself, through a multicall.
        native(
            LP_SENT_TX, WALLET, V3_MANAGER, 0,
            at="2026-09-18T16:16:07.000000Z", method="multicall", fee=4218842563763,
        ),
        erc20(LP_SENT_TX, V3_MANAGER, WALLET, 48367021, USDC_TOKEN,
              at="2026-09-18T16:16:07.000000Z", method="0xac9650d8"),
    ]


REAL_TRANSFER_TX = "0xa05fc00d" + "0" * 56


def poisoned_day() -> list[dict[str, Any]]:
    """0xB26B… on Base, 10 Aug 2026: one real payment and the poisoning after it."""
    at = "2026-08-10T16:{:02d}:00.000000Z"
    return [
        # A fake "USDC" (Armenian letters) "sent" from the wallet to a lookalike
        # of the real payee, by the poisoner's contract: the wallet sent nothing.
        erc20(
            "0xacdebe53" + "0" * 56, OTHER_WALLET, LOOKALIKE_1, 991238129,
            token(FAKE_USDC_1, "ՍՏDС", 6), at=at.format(56), method="0xa9059cbb",
        ),
        # Real USDC dust, from a lookalike.
        erc20(
            "0xd330714a" + "0" * 56, LOOKALIKE_1, OTHER_WALLET, 99, USDC_TOKEN,
            at=at.format(35), method="0xa9059cbb",
        ),
        erc20(
            "0x4750c114" + "0" * 56, OTHER_WALLET, LOOKALIKE_2, 991238129,
            token(FAKE_USDC_2, "UṢDC", 6), at=at.format(29), method="0x7111a994",
        ),
        # A zero-value real-USDC transferFrom out of the wallet, to a lookalike.
        erc20(
            "0x5d1e0b7a" + "0" * 56, OTHER_WALLET, ZERO_VALUE_LOOKALIKE, 0, USDC_TOKEN,
            at=at.format(27), method="0x23b872dd",
        ),
        # The real payment, sent by the wallet itself.
        native(
            REAL_TRANSFER_TX, OTHER_WALLET, USDC, 0,
            at=at.format(25), method="transfer", fee=579010223403,
        ),
        erc20(
            REAL_TRANSFER_TX, OTHER_WALLET, REAL_PAYEE, 991238129, USDC_TOKEN,
            at=at.format(25), method="0xa9059cbb", receiver_contract=False,
        ),
        erc20(
            "0x6f00aa11" + "0" * 56, ZERO_VALUE_REAL, OTHER_WALLET, 5_000_000, USDC_TOKEN,
            at=at.format(20), method="0xa9059cbb", sender_contract=False,
        ),
    ]
