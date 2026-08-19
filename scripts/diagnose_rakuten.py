#!/usr/bin/env python3
"""楽天APIの接続診断。

2026年の新APIは呼び出し元を厳密に検査するため、403 で弾かれたときに
「何が原因か」が分かりにくい。このスクリプトは Origin 候補を順に試し、
どれが通るかを実測する。推測で config を書き換えないための道具。

    .venv/bin/python scripts/diagnose_rakuten.py

認証情報は一切出力しない。出るのは Origin候補・HTTPステータス・
楽天が返した errorMessage だけ。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src.config import load_config  # noqa: E402
from src.rakuten.client import ITEM_FIELDS  # noqa: E402

# 試す Origin の候補。
# Origin ヘッダは本来 scheme + host のみでパスを含まないため、
# パス付きは通らない可能性が高い（それも含めて実測する）。
ORIGIN_CANDIDATES = [
    "https://ryota-sotoguchi.github.io",
    "https://ryota-sotoguchi.github.io/cosme",
    "https://github.com",
    "https://github.com/Ryota-Sotoguchi/cosme",
]

ERROR_HINTS = {
    "CLIENT_IP_NOT_ALLOWED": (
        "アプリが「許可IPアドレス」照合モードになっている。\n"
        "     → 楽天デベロッパーコンソールでアプリ種別を「Web Application」に変更し、\n"
        "       「許可Webサイト」に Origin のホスト名を登録する（許可IP欄は空に）。"
    ),
    "HTTP_REFERRER_NOT_ALLOWED": (
        "Origin/Referer が「許可Webサイト」と一致していない。\n"
        "     → 登録値と完全一致させる（https:// の有無、末尾スラッシュに注意）。"
    ),
    "INVALID_ACCESS_KEY": "accessKey が違う。デベロッパーコンソールで再確認する。",
    "INVALID_APPLICATION_ID": "applicationId が違う。",
    "TOO_MANY_REQUESTS": "レート制限。1秒以上あけて再実行する。",
}


def describe(payload: dict) -> tuple[str, str]:
    """レスポンスから errorMessage と件数の説明を取り出す。"""
    errors = payload.get("errors")
    if errors:
        if isinstance(errors, list):
            errors = errors[0] if errors else {}
        return str(errors.get("errorMessage", "?")), ""
    if "error" in payload:
        return str(payload.get("error")), str(payload.get("error_description", ""))
    count = payload.get("count")
    hits = len(payload.get("Items") or [])
    return "", f"{hits}件取得 (総ヒット数 {count})"


def main() -> int:
    config = load_config(dry_run=True)
    creds = config.credentials

    print("\n楽天API 接続診断")
    print("=" * 62)

    missing = [
        name
        for name, value in (
            ("RAKUTEN_APPLICATION_ID", creds.rakuten_application_id),
            ("RAKUTEN_ACCESS_KEY", creds.rakuten_access_key),
            ("RAKUTEN_AFFILIATE_ID", creds.rakuten_affiliate_id),
        )
        if not value
    ]
    if missing:
        print(f"\n❌ .env に未設定の項目があります: {', '.join(missing)}\n")
        return 1

    print("認証情報: 3項目とも設定済み（値は表示しません）")
    print(f"設定中の Origin: {config.rakuten_origin}")

    endpoints = [config.rakuten["endpoint"], *config.rakuten.get("fallback_endpoints", [])]
    params = {
        "applicationId": creds.rakuten_application_id,
        "affiliateId": creds.rakuten_affiliate_id,
        "format": "json",
        "formatVersion": 2,
        "elements": ITEM_FIELDS,
        "genreId": config.genres[0]["id"],
        "hits": 3,
    }

    succeeded: list[tuple[str, str, str]] = []

    for endpoint in endpoints:
        version = endpoint.rsplit("/", 1)[-1]
        print(f"\n--- エンドポイント {version} ---")

        for origin in ORIGIN_CANDIDATES:
            for mode in ("header", "query"):
                request_params = dict(params)
                headers = {
                    "Accept": "application/json",
                    "User-Agent": config.rakuten.get("user_agent", "cosme-bot/1.0"),
                    "Origin": origin,
                    "Referer": origin.rstrip("/") + "/",
                }
                if mode == "header":
                    headers["accessKey"] = creds.rakuten_access_key or ""
                else:
                    request_params["accessKey"] = creds.rakuten_access_key

                time.sleep(1.2)  # 1秒1リクエスト以下を守る
                try:
                    response = requests.get(
                        endpoint, params=request_params, headers=headers, timeout=(10, 20)
                    )
                except requests.RequestException as exc:
                    print(f"  {origin:44s} {mode:6s} 接続失敗 {type(exc).__name__}")
                    continue

                try:
                    payload = response.json()
                except json.JSONDecodeError:
                    payload = {}

                message, detail = describe(payload)
                status = response.status_code

                if status == 200 and not message:
                    print(f"  ✅ {origin:44s} {mode:6s} 200  {detail}")
                    succeeded.append((endpoint, origin, mode))
                else:
                    print(f"  ❌ {origin:44s} {mode:6s} {status}  {message or detail}")
                    hint = ERROR_HINTS.get(message)
                    if hint and not succeeded:
                        print(f"     → {hint}")
                        # 同じ原因なら他の候補も同じ結果になるので、IP系は即打ち切る
                        if message == "CLIENT_IP_NOT_ALLOWED":
                            print("\n" + "=" * 62)
                            print("IP照合モードのままです。上記の設定変更が必要です。")
                            print("=" * 62 + "\n")
                            return 2

        if succeeded:
            break

    print("\n" + "=" * 62)
    if succeeded:
        endpoint, origin, mode = succeeded[0]
        print("✅ 接続できる組み合わせが見つかりました\n")
        print(f"  エンドポイント : {endpoint}")
        print(f"  Origin        : {origin}")
        print(f"  accessKey     : {mode}")
        print(f"\n.env の RAKUTEN_ORIGIN をこの値にしてください:\n  {origin}\n")
        return 0

    print("❌ どの組み合わせでも接続できませんでした。")
    print("   楽天デベロッパーコンソールのアプリ設定を確認してください。\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
