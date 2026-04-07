import time
import yfinance as yf
from yfinance.exceptions import YFRateLimitError


def fetch_history(ticker: str, period: str = "1d", interval: str = "1m",
                  max_retries: int = 5, backoff: float = 10.0):
    """
    Fetch historical price data with retry on rate limit.

    :param ticker:       Stock symbol, e.g. "MSFT"
    :param period:       Data period, e.g. "1d", "5d", "1mo"
    :param interval:     Data interval, e.g. "1m", "5m", "1h", "1d"
    :param max_retries:  Maximum number of retries on rate-limit errors
    :param backoff:      Initial wait time (seconds) between retries (doubles each time)
    :return:             pandas DataFrame with OHLCV data
    """
    t = yf.Ticker(ticker)
    wait = backoff

    for attempt in range(1, max_retries + 1):
        try:
            df = t.history(period=period, interval=interval)
            if df.empty:
                print(f"[WARN] No data returned for {ticker} "
                      f"(period={period}, interval={interval})")
            return df
        except YFRateLimitError:
            if attempt == max_retries:
                raise
            print(f"[WARN] Rate limited (attempt {attempt}/{max_retries}). "
                  f"Retrying in {wait:.0f}s ...")
            time.sleep(wait)
            wait *= 2   # exponential backoff


def main():
    symbol   = "MSFT"
    period   = "1d"
    interval = "1m"

    print(f"Fetching {symbol} | period={period} | interval={interval} ...")
    df = fetch_history(symbol, period=period, interval=interval)

    if df is not None and not df.empty:
        print(f"\n--- {symbol} last {len(df)} bars ---")
        print(df.tail(10).to_string())

        print(f"\nLatest close : {df['Close'].iloc[-1]:.4f}")
        print(f"Daily high   : {df['High'].max():.4f}")
        print(f"Daily low    : {df['Low'].min():.4f}")
        print(f"Total volume : {df['Volume'].sum():,.0f}")
    else:
        print("No data available.")


if __name__ == "__main__":
    main()

