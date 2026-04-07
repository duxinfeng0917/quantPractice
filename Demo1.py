"""
Demo1.py — Stock data fetcher (US equities via akshare)
No API key required. Data source: EastMoney (东方财富).
"""

import datetime
import akshare as ak


def get_us_symbol(ticker: str) -> str:
    """
    Resolve a plain ticker (e.g. "MSFT") to the akshare format (e.g. "105.MSFT").
    Searches the akshare US-stock name table for an exact match.
    Falls back to the NASDAQ prefix 105 if not found.
    """
    try:
        df_all = ak.stock_us_spot_em()   # full list of US stocks on EastMoney
        # The name table has columns like '代码', '名称'
        code_col = next(c for c in df_all.columns if "代码" in c)
        match = df_all[df_all[code_col].str.endswith("." + ticker.upper())]
        if not match.empty:
            return match.iloc[0][code_col]
    except Exception:
        pass
    # Default: NASDAQ prefix
    return f"105.{ticker.upper()}"


def fetch_history(ticker: str, days: int = 30) -> "pd.DataFrame":
    """Fetch daily OHLCV data via akshare (EastMoney backend)."""
    symbol     = get_us_symbol(ticker)
    end_date   = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    print(f"[INFO] akshare symbol resolved: {symbol}")
    df = ak.stock_us_hist(
        symbol     = symbol,
        period     = "daily",
        start_date = start_date.strftime("%Y%m%d"),
        end_date   = end_date.strftime("%Y%m%d"),
        adjust     = "qfq",
    )
    return df


def main():
    symbol = "MSFT"
    days   = 30

    print(f"Fetching {symbol} | last {days} calendar days ...")
    df = fetch_history(symbol, days=days)

    if df is None or df.empty:
        print("[ERROR] No data returned.")
        return

    print(f"\nColumns: {list(df.columns)}")
    print(f"\n--- {symbol} last {len(df)} trading days ---")
    print(df.tail(10).to_string(index=False))

    # Map Chinese column names to standard labels
    col_map = {}
    for col in df.columns:
        lc = col.lower()
        if "收盘" in col or lc == "close":
            col_map["close"] = col
        elif "最高" in col or lc == "high":
            col_map["high"] = col
        elif "最低" in col or lc == "low":
            col_map["low"] = col
        elif "成交量" in col or lc == "volume":
            col_map["volume"] = col

    if "close" in col_map:
        print(f"\nLatest close : {df[col_map['close']].iloc[-1]:.4f}")
    if "high" in col_map:
        print(f"Period high  : {df[col_map['high']].max():.4f}")
    if "low" in col_map:
        print(f"Period low   : {df[col_map['low']].min():.4f}")
    if "volume" in col_map:
        print(f"Avg volume   : {df[col_map['volume']].mean():,.0f}")


if __name__ == "__main__":
    main()




