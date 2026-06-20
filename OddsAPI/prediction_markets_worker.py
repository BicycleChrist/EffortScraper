import re
import threading
import time
from PyQt6.QtCore import QThread, pyqtSignal

class PredictionMarketsWorker(QThread):
    """Background worker to fetch prediction market data without blocking UI"""

    # Signals for communication with main thread
    data_ready = pyqtSignal(list)  # Emits formatted prediction market data
    error_occurred = pyqtSignal(str)  # Emits error messages
    status_update = pyqtSignal(str)  # Emits status updates

    def __init__(self):
        super().__init__()
        self.should_stop = False
        self.update_interval = 1500  # 25 minutes between updates
        # Sleeping between updates parks on this event instead of polling
        # time.sleep(1) slices — stop() sets it, so the worker wakes (and
        # exits) immediately instead of up to 1s later. That 1s lag was a
        # ~125ms+ main-loop stall in closeEvent's wait().
        self._stop_event = threading.Event()

    def run(self):
        """Main worker loop - runs in background thread"""
        # Shared cancellation flag passed into polymarketquery so it can bail
        # out of pagination / volume fetches when the worker is asked to stop.
        self.cancellation_flag = {'should_stop': False}

        while not self.should_stop:
            try:
                self.status_update.emit("Fetching prediction markets...")

                try:
                    from polymarketquery import fetch_and_process_markets

                    if self.should_stop:
                        break

                    markets_data = fetch_and_process_markets(
                        recent_only=True,
                        cancellation_flag=self.cancellation_flag,
                    )

                    if self.should_stop:
                        break

                    if markets_data:
                        formatted_markets = self.format_for_tickertape(markets_data)
                        if formatted_markets and not self.should_stop:
                            self.data_ready.emit(formatted_markets)
                            self.status_update.emit(f"Loaded {len(formatted_markets)} prediction markets")
                        else:
                            self.status_update.emit("No markets with sufficient volume found")
                    else:
                        if not self.should_stop:
                            self.status_update.emit("No prediction market data received")

                except ImportError as e:
                    self.error_occurred.emit(f"Failed to import polymarketquery: {e}")
                except Exception as e:
                    if not self.should_stop:
                        self.error_occurred.emit(f"Error fetching markets: {e}")

                # Wait for next update or stop signal (returns immediately
                # once stop() sets the event)
                self._stop_event.wait(self.update_interval)

            except Exception as e:
                self.error_occurred.emit(f"Unexpected error in prediction markets worker: {e}")
                self._stop_event.wait(30)

    @staticmethod
    def _lead_prob(market):
        """Highest outcome probability (0-100) from lines like 'Yes: 18.05%'."""
        ps = []
        for ln in market.get('lines', []):
            try:
                ps.append(float(ln.split(':')[1].strip().rstrip('%')))
            except (IndexError, ValueError):
                pass
        return max(ps) if ps else None

    @staticmethod
    def _family_key(market):
        """Collapse 'Will <subject> win <thing>' so only ONE of a futures family
        (e.g. the ~30 'Will <country> win the 2026 World Cup?' markets) shows."""
        q = (market.get('question') or '').lower()
        q = re.sub(r"^will\s+[a-z0-9 .'\-&]+?\s+win\s+", 'win ', q)
        return q[:45]

    def format_for_tickertape(self, markets_data):
        """Pick a varied set of solid prediction-market headlines.

        Ranks by RECENT (24h) activity with lifetime volume as a tiebreaker, drops
        near-certain (>97%) and dead-longshot (<3%) markets (no headline interest),
        and de-dupes so the ticker isn't 15 near-identical futures — capped per
        question-family and per category so topics stay varied.
        """
        ranked = sorted(
            markets_data,
            key=lambda m: (m.get('total_volume_24hr', 0) or 0,
                           m.get('total_volume', 0) or 0),
            reverse=True)

        def short(pct_str):
            try:
                return f"{float(str(pct_str).rstrip('%')):.0f}%"
            except (ValueError, TypeError):
                return pct_str

        formatted_markets = []
        seen_family = set()
        cat_count = {}

        for market in ranked:
            if len(formatted_markets) >= 15:
                break
            try:
                lp = self._lead_prob(market)
                # Skip near-locked (>97%) and dead longshots (<3%).
                if lp is None or lp > 97 or lp < 3:
                    continue

                family = self._family_key(market)
                if family in seen_family:
                    continue
                cat = (market.get('tags') or ['misc'])[0]
                if cat_count.get(cat, 0) >= 4:   # keep categories varied
                    continue

                question = market.get('question', 'Unknown Market')
                yes_price = no_price = None
                top_name, top_val = None, None
                for line in market.get('lines', []):
                    try:
                        name, val = line.split(':', 1)
                        name, val = name.strip(), val.strip()
                        fv = float(val.rstrip('%'))
                    except (ValueError, IndexError):
                        continue
                    if name.lower() == 'yes':
                        yes_price = val
                    elif name.lower() == 'no':
                        no_price = val
                    if top_val is None or fv > top_val:
                        top_val, top_name = fv, name

                if yes_price is not None and no_price is not None:
                    price_str = f"Yes {short(yes_price)} / No {short(no_price)}"
                elif top_name is not None:
                    price_str = f"{top_name} {top_val:.0f}%"
                else:
                    continue  # no usable prices

                display_q = question if len(question) <= 60 else question[:57] + "..."
                vol = market.get('total_volume', 0) or 0
                if vol >= 1_000_000:
                    vol_str = f"  ·  ${vol/1e6:.0f}M"
                elif vol >= 1000:
                    vol_str = f"  ·  ${vol/1e3:.0f}K"
                else:
                    vol_str = ""

                seen_family.add(family)
                cat_count[cat] = cat_count.get(cat, 0) + 1
                formatted_markets.append(f"{display_q}  —  {price_str}{vol_str}")

            except Exception as e:
                print(f"Error formatting market {market.get('question', 'Unknown')}: {e}")
                continue

        return formatted_markets

    def stop(self):
        """Signal the worker to stop gracefully"""
        self.should_stop = True
        if hasattr(self, 'cancellation_flag'):
            self.cancellation_flag['should_stop'] = True
        self._stop_event.set()
        self.quit()
        # The event wakes the inter-update sleep instantly, so this returns
        # in single-digit ms unless the worker is mid-HTTP-fetch; the app is
        # exiting at this point so we don't need to confirm termination.
        if not self.wait(500):
            self.terminate()
